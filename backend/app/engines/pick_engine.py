"""Pick Engine — the main decision layer.

Statuses: BET / WAIT / PASS / MISSED / EXPIRED.
"BET" NEVER means guaranteed. It means: every configured rule passed at scan time.

Decision rules (spec, all must pass for BET):
 1. EV >= settings.min_ev_pct (model prob beats de-vigged book implied)
 2. market inside acceptable rule (ML, or spread no worse than max_spread)
 3. current odds >= minimum acceptable odds
 4. execution window still valid (not past expiry / live+window)
 5. data quality acceptable
 6. daily/weekly/drawdown limits not breached
 7. sportsbook/account limit supports the stake
 +D16 guardrail: <min_verified_history settled verified bets caps status at WAIT
   unless friend+model consensus.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bet, Match, OddsSnapshot, Pick, Recommendation, Settings
from . import odds_math
from .identity import alias_uncertainty, canonical_name
from .research import similar_setups
from .signals import all_signals, combine_signals
from .steam import steam_prediction_for_snapshot

MODEL_VERSION = "pick_engine_v1"
FEATURE_SET_VERSION = "fs1"

# Reason codes (spec list)
RC = ("PLAYER_EDGE H2H_EDGE FORM_EDGE ELO_EDGE MARKET_MISPRICE LIVE_ODDS_JUMP "
      "STALE_LINE GOOD_PRICE BAD_PRICE LIMIT_TOO_LOW WINDOW_MISSED DATA_WEAK NO_BET "
      "PRE_KICK_STEAM STEAM_DATA_WEAK STEAM_AGAINST").split()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rank_score(ev_pct: float, model_conf: float, data_quality: float,
               movement_signal: float, urgency: float, similar_roi: float | None,
               stake_feasible: float) -> float:
    """TRANSPARENT ranking formula (spec: do not hide it).

    score = 40 * clamp(ev_pct / 15)            # EV dominates, saturates at 15%
          + 20 * model_conf                    # ensemble confidence 0..1
          + 15 * data_quality                  # 0..1
          + 10 * clamp01((movement+1)/2)       # market moving our way
          +  5 * urgency                       # closer to live window = act now
          +  5 * clamp((similar_roi or 0)/20)  # historical ROI of similar setups, sat at +20%
          +  5 * stake_feasible                # 1 if limit supports stake, else fraction
    Max 100. Every term auditable in signals_json.
    """
    c = lambda v: max(0.0, min(1.0, v))
    return round(
        40 * c(ev_pct / 15.0)
        + 20 * c(model_conf)
        + 15 * c(data_quality)
        + 10 * c((movement_signal + 1) / 2)
        + 5 * c(urgency)
        + 5 * c((similar_roi or 0.0) / 20.0)
        + 5 * c(stake_feasible), 2)


def _risk_breached(db: Session, s: Settings) -> bool:
    now = _now()
    bets = db.scalars(select(Bet).where(Bet.result.in_(("win", "loss", "push")))).all()
    day = sum(b.profit for b in bets if b.placed_at >= now - timedelta(days=1))
    week = sum(b.profit for b in bets if b.placed_at >= now - timedelta(days=7))
    if day <= -s.max_daily_loss or week <= -s.max_weekly_loss:
        return True
    bank = s.starting_bankroll + sum(b.profit for b in bets)
    peak = s.starting_bankroll
    run = s.starting_bankroll
    for b in sorted(bets, key=lambda x: x.placed_at):
        run += b.profit
        peak = max(peak, run)
    dd = (peak - bank) / peak * 100 if peak > 0 else 0
    return dd >= s.max_drawdown_shutdown_pct


def _verified_settled_count(db: Session) -> int:
    return len(db.scalars(select(Bet.id).where(
        Bet.result.in_(("win", "loss", "push")),
        Bet.data_source.notin_(("manual_seed", "synthetic_demo", "seed")))).all())


def _latest_prices(db: Session, match_id: int) -> list[OddsSnapshot]:
    """Latest snapshot per (book, market, selection, line)."""
    snaps = db.scalars(select(OddsSnapshot).where(OddsSnapshot.match_id == match_id)
                       .order_by(OddsSnapshot.collected_at)).all()
    latest: dict = {}
    for sn in snaps:
        latest[(sn.sportsbook, sn.market, sn.selection, sn.line)] = sn
    return list(latest.values())


def _friend_rec(db: Session, m: Match) -> Recommendation | None:
    return db.scalar(select(Recommendation).where(
        Recommendation.match_id == m.id, Recommendation.source_name != "model"))


def evaluate_match(db: Session, m: Match, s: Settings, verified_n: int,
                   risk_breached: bool, nu: float = 0.63) -> list[dict]:
    """All candidate picks (ML + acceptable spreads, every book) for one match."""
    ens = combine_signals(all_signals(db, m, nu=nu))
    sig_by = {x["name"]: x for x in ens["signals"]}
    rec = _friend_rec(db, m)
    now = _now()
    out = []

    for sn in _latest_prices(db, m.id):
        if sn.market == "SPREAD_2WAY" and sn.line is not None and sn.line < -0.5:
            continue  # rule: spread only up to -0.5
        if sn.market not in ("ML_3WAY", "SPREAD_2WAY"):
            continue
        if sn.selection not in ("home", "away", "draw"):
            continue
        p = {"home": ens["p_home"], "draw": ens["p_draw"], "away": ens["p_away"]}[sn.selection]
        ev = odds_math.expected_value(p, sn.decimal_odds)
        ev_pct = round(ev * 100, 2)

        reasons: list[str] = []
        elo_s = sig_by["elo"]
        if elo_s["pick"] == sn.selection and elo_s["confidence"] > 0.15:
            reasons.append("ELO_EDGE")
        if sig_by["form"]["pick"] == sn.selection and sig_by["form"]["quality"] > 0.5:
            reasons.append("FORM_EDGE")
        if sig_by["h2h"]["pick"] == sn.selection and sig_by["h2h"]["quality"] > 0.3:
            reasons.append("H2H_EDGE")
        mv = sig_by["movement"]
        mv_signal = 0.0
        if mv["quality"] > 0:
            mv_signal = (mv["p_home"] - mv["p_away"]) / 0.24  # invert tilt to [-1,1]
            if sn.selection == "away":
                mv_signal = -mv_signal
            if abs(mv_signal) > 0.5:
                reasons.append("LIVE_ODDS_JUMP")
        if ev_pct >= s.min_ev_pct:
            reasons.append("MARKET_MISPRICE")
        age_min = (now - sn.collected_at).total_seconds() / 60
        if age_min > 30:
            reasons.append("STALE_LINE")

        # Pre-kickoff steam prediction is separate from win probability.
        steam = steam_prediction_for_snapshot(db, m, sn, s)
        reasons.extend(steam.get("reason_codes", []))

        # confidence breakdown (spec: never one number)
        model_conf = max(ens["p_home"], ens["p_away"]) - min(ens["p_home"], ens["p_away"])
        data_q = ens["total_weight"] / sum((0.30, 0.15, 0.10, 0.20, 0.15, 0.10))
        exec_conf = 1.0 if m.start_time > now else max(0.0, 1 - (now - m.start_time).total_seconds() / max(1, s.exec_window_seconds))
        market_conf = 1.0 - min(1.0, age_min / 60)
        steam_conf = steam.get("quality", 0.0) * abs((steam.get("steam_probability", 0.5) - 0.5) * 2)
        penalties = []
        if alias_uncertainty(db, m.home_player_id) or alias_uncertainty(db, m.away_player_id):
            penalties.append("player alias uncertain"); data_q *= 0.7
        if ens["disagreement"] > 0.5:
            penalties.append("high model disagreement"); model_conf *= 0.7
        sim = similar_setups(db, m.home_player_id, m.away_player_id, m.league,
                             sn.decimal_odds, sn.market, before=m.start_time)
        hist_conf = min(1.0, sim["n"] / max(1, s.min_similar_sample))
        if sim["n"] < s.min_similar_sample:
            penalties.append(f"similar-setup sample {sim['n']} < {s.min_similar_sample}")
        if steam.get("quality", 0.0) < 0.25:
            penalties.append("pre-kick steam history weak")
        conf = {
            "model": round(model_conf, 3), "market": round(market_conf, 3),
            "execution": round(exec_conf, 3), "data_quality": round(data_q, 3),
            "historical": round(hist_conf, 3), "steam": round(steam_conf, 3),
            "overall": round(model_conf * 0.30 + market_conf * 0.12 + exec_conf * 0.13
                             + data_q * 0.18 + hist_conf * 0.12 + steam_conf * 0.15, 3),
            "penalties": penalties,
        }

        # consensus (spec)
        friend_side = None
        if rec and rec.recommended_selection:
            cn = canonical_name(rec.recommended_selection)
            friend_side = "home" if cn == m.home_player.name else "away" if cn == m.away_player.name else None
        model_side = "home" if ens["p_home"] > ens["p_away"] else "away"
        if friend_side and friend_side == model_side == sn.selection:
            consensus = "friend_model_agree"
        elif friend_side and friend_side != model_side and sn.selection in (friend_side, model_side):
            consensus = "conflict"
        elif friend_side and sn.selection == friend_side:
            consensus = "friend_only"
        elif ens["disagreement"] < 0.34 and sn.selection == model_side:
            consensus = "strong_consensus"
        else:
            consensus = "model_only"

        # ---- decision rules ----
        status = "WAIT"
        min_am = rec.min_american_odds if rec and rec.min_american_odds else None
        live = m.start_time <= now
        window_end = m.start_time + timedelta(seconds=s.exec_window_seconds)
        expired_rec = rec and rec.expires_at and now > rec.expires_at

        if m.home_score is not None:
            status = "EXPIRED"
        elif expired_rec:
            status = "EXPIRED"; reasons.append("NO_BET")
        elif live and now > window_end:
            status = "MISSED"; reasons.append("WINDOW_MISSED")
        elif ev_pct < s.min_ev_pct:
            status = "PASS"; reasons.append("BAD_PRICE" if ev_pct < 0 else "NO_BET")
        elif min_am is not None and sn.american_odds < min_am:
            status = "PASS"; reasons.append("BAD_PRICE")
        elif risk_breached:
            status = "PASS"; reasons.append("NO_BET"); penalties.append("risk limits breached")
        elif steam.get("status") == "drift_likely" and steam.get("steam_probability", 0.5) < 0.42:
            status = "PASS"; reasons.append("STEAM_AGAINST")
        else:
            # candidate BET — remaining rules
            stake = _suggest_stake(s, p, sn.decimal_odds)
            limit = rec.limit_seen if rec and rec.limit_seen else None
            feasible = 1.0 if (limit is None or limit >= stake) else limit / stake
            if feasible < 1.0:
                status = "PASS"; reasons.append("LIMIT_TOO_LOW")
            elif conf["data_quality"] < 0.25:
                status = "WAIT"; reasons.append("DATA_WEAK")
            elif verified_n < s.min_verified_history and consensus != "friend_model_agree":
                status = "WAIT"; reasons.append("DATA_WEAK")  # D16 guardrail
            else:
                status = "BET"; reasons.append("GOOD_PRICE")
                if not live:
                    status = "BET"  # place-when-live workflow shown via window fields

        stake = _suggest_stake(s, p, sn.decimal_odds)
        limit = rec.limit_seen if rec and rec.limit_seen else None
        feasible = 1.0 if (limit is None or limit >= stake) else round(limit / stake, 2)
        urgency = 0.0
        if not live and m.start_time - now < timedelta(minutes=10):
            urgency = 1 - (m.start_time - now).total_seconds() / 600
        elif live and now <= window_end:
            urgency = 1.0

        score = rank_score(ev_pct, conf["overall"], conf["data_quality"],
                           mv_signal, urgency, sim.get("roi_pct"), feasible)

        out.append({
            "match_id": m.id, "match": f"{m.home_player.name} vs {m.away_player.name}",
            "league": m.league, "scheduled_start": m.start_time.isoformat(),
            "market": sn.market, "selection": sn.selection, "line": sn.line,
            "sportsbook": sn.sportsbook,
            "current_american": sn.american_odds, "current_decimal": sn.decimal_odds,
            "min_american_odds": min_am,
            "ideal_american_odds": rec.ideal_american_odds if rec else None,
            "acceptable_alt": "SPREAD_2WAY up to -0.5" if sn.market == "ML_3WAY" else "ML_3WAY",
            "max_spread": rec.max_spread if rec and rec.max_spread is not None else -0.5,
            "model_prob": round(p, 4), "fair_decimal": round(1 / p, 3) if p > 0 else None,
            "ev_pct": ev_pct, "rank_score": score, "status": status,
            "reason_codes": sorted(set(reasons)) or ["NO_BET"],
            "confidence": conf, "consensus": consensus,
            "suggested_stake": stake, "limit_seen": limit,
            "exec_window_seconds": s.exec_window_seconds,
            "expires_at": (rec.expires_at.isoformat() if rec and rec.expires_at
                           else window_end.isoformat()),
            "similar_setups": sim,
            "steam": steam,
            "predicted_first_live_american": steam.get("predicted_first_live_american"),
            "predicted_first_live_decimal": steam.get("predicted_first_live_decimal"),
            "steam_probability": steam.get("steam_probability"),
            "expected_line_movement_cents": steam.get("expected_line_movement_cents"),
            "maximum_entry_price": steam.get("maximum_entry_price"),
            "execution_window": steam.get("execution_window"),
            "signals": ens["signals"], "disagreement": ens["disagreement"],
            "recommendation_id": rec.id if rec else None,
            "seed_influenced": bool(rec and rec.verification_status == "seed_partial"),
        })
    return out


def _suggest_stake(s: Settings, p: float, dec: float) -> float:
    kelly = odds_math.kelly_fraction(p, dec) * s.kelly_fraction
    stake = max(0.0, min(s.max_bet_size, round(kelly * s.starting_bankroll, 2)))
    return stake or s.unit_size


def generate_picks(db: Session, persist: bool = True, nu: float = 0.63) -> list[dict]:
    """Scan all upcoming ESoccer matches × markets × books; rank; persist Pick rows
    (never overwritten — new scan = new rows, spec: model versioning)."""
    s = db.get(Settings, 1)
    now = _now()
    risk = _risk_breached(db, s)
    vn = _verified_settled_count(db)
    upcoming = db.scalars(select(Match).where(
        Match.home_score.is_(None), Match.start_time > now - timedelta(minutes=10))).all()
    cards: list[dict] = []
    for m in upcoming:
        cards.extend(evaluate_match(db, m, s, vn, risk, nu=nu))
    cards.sort(key=lambda c: -c["rank_score"])
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    if persist:
        for c in cards:
            db.add(Pick(
                match_id=c["match_id"], market=c["market"], selection=c["selection"],
                line=c["line"], sportsbook=c["sportsbook"],
                current_american=c["current_american"], current_decimal=c["current_decimal"],
                min_american_odds=c["min_american_odds"], ideal_american_odds=c["ideal_american_odds"],
                model_prob=c["model_prob"], fair_decimal=c["fair_decimal"] or 0.0,
                ev_pct=c["ev_pct"], rank_score=c["rank_score"], status=c["status"],
                reason_codes=json.dumps(c["reason_codes"]),
                confidence_json=json.dumps(c["confidence"]),
                signals_json=json.dumps({"signals": c["signals"], "similar": c["similar_setups"], "steam": c["steam"]}),
                suggested_stake=c["suggested_stake"],
                expires_at=datetime.fromisoformat(c["expires_at"]),
                exec_window_seconds=c["exec_window_seconds"],
                model_version=MODEL_VERSION, feature_set_version=FEATURE_SET_VERSION,
                consensus=c["consensus"], recommendation_id=c["recommendation_id"],
                include_in_metrics=not c["seed_influenced"],
            ))
        db.commit()
    return cards


def sweep_and_settle(db: Session) -> dict:
    """Expire stale picks; settle picks whose match finished; grade them."""
    from .research import grade_pick
    now = _now()
    changed = {"expired": 0, "missed": 0, "settled": 0}
    for p in db.scalars(select(Pick).where(Pick.settled_result.is_(None))).all():
        m = db.get(Match, p.match_id)
        if m and m.home_score is not None:
            won = (m.winner == p.selection) if p.market == "ML_3WAY" else _spread_won(m, p)
            p.settled_result = "win" if won else "loss"
            if p.user_decision == "bet" and p.suggested_stake:
                p.profit = round(p.suggested_stake * (p.current_decimal - 1), 2) if won else -p.suggested_stake
            close = db.scalars(select(OddsSnapshot).where(
                OddsSnapshot.match_id == m.id, OddsSnapshot.market == p.market,
                OddsSnapshot.selection == p.selection, OddsSnapshot.is_closing.is_(True))
                .order_by(OddsSnapshot.collected_at.desc())).first()
            if close and p.current_decimal:
                p.closing_american = close.american_odds
                p.clv_pct = odds_math.clv_pct(p.current_decimal, close.decimal_odds)
            p.grade = grade_pick(p)
            changed["settled"] += 1
        elif p.expires_at and now > p.expires_at and p.status in ("BET", "WAIT"):
            new = "MISSED" if p.status == "BET" else "EXPIRED"
            p.status = new
            changed["missed" if new == "MISSED" else "expired"] += 1
    db.commit()
    return changed


def _spread_won(m: Match, p: Pick) -> bool:
    if m.home_score is None:
        return False
    margin = (m.home_score - m.away_score) if p.selection == "home" else (m.away_score - m.home_score)
    return margin + (p.line or 0.0) > 0
