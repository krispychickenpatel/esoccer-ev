"""Backtester.

For each finished match with odds snapshots in the window:
  1. Get each player's Elo AS OF the snapshot timestamp (rating_history).
  2. Compute model probabilities (Davidson).
  3. If EV and confidence pass filters, place a simulated bet at snapshot odds.
  4. Settle against the actual result; track bankroll.

One bet max per match+selection (uses the LAST qualifying snapshot before
kickoff — closest to what you could actually bet). No lookahead: ratings come
from matches that finished strictly before the snapshot.

Known simplification: assumes full requested stake is matched at the listed
price. Real ESoccer limits are low; treat backtest ROI as an upper bound.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, OddsSnapshot
from . import odds_math
from .elo import win_draw_loss_probs
from .ratings import elo_as_of, matches_played_before

SEL_IDX = {"home": 0, "draw": 1, "away": 2}


@dataclass
class BacktestConfig:
    date_from: datetime | None = None
    date_to: datetime | None = None
    market: str = "ML_3WAY"
    min_ev_pct: float = 5.0
    min_confidence: float = 0.0
    player_ids: list[int] = field(default_factory=list)   # empty = all
    min_decimal: float = 1.01
    max_decimal: float = 100.0
    stake_mode: str = "flat"          # 'flat' or 'kelly'
    flat_stake: float = 10.0
    kelly_fraction: float = 0.25
    starting_bankroll: float = 1000.0
    nu: float = 0.63


def run_backtest(db: Session, cfg: BacktestConfig) -> dict:
    q = (
        select(Match)
        .where(Match.home_score.is_not(None))
        .order_by(Match.start_time, Match.id)
    )
    if cfg.date_from:
        q = q.where(Match.start_time >= cfg.date_from)
    if cfg.date_to:
        q = q.where(Match.start_time <= cfg.date_to)
    matches = db.scalars(q).all()

    bankroll = cfg.starting_bankroll
    peak = bankroll
    max_dd = 0.0
    curve: list[dict] = [{"t": None, "bankroll": bankroll}]
    bets: list[dict] = []
    losing_streak = longest_losing = 0

    for m in matches:
        if cfg.player_ids and not (
            m.home_player_id in cfg.player_ids or m.away_player_id in cfg.player_ids
        ):
            continue

        snaps = db.scalars(
            select(OddsSnapshot)
            .where(OddsSnapshot.match_id == m.id,
                   OddsSnapshot.market == cfg.market,
                   OddsSnapshot.collected_at < m.start_time)
            .order_by(OddsSnapshot.collected_at, OddsSnapshot.id)
        ).all()
        if not snaps:
            continue

        # last pre-kickoff snapshot per (book, selection, line)
        last: dict[tuple, OddsSnapshot] = {}
        for s in snaps:
            last[(s.sportsbook, s.selection, s.line)] = s

        elo_h = elo_as_of(db, m.home_player_id, m.start_time)
        elo_a = elo_as_of(db, m.away_player_id, m.start_time)
        n_h = min(matches_played_before(db, m.home_player_id, m.start_time), 25) / 25.0
        n_a = min(matches_played_before(db, m.away_player_id, m.start_time), 25) / 25.0
        confidence = (n_h + n_a) / 2.0
        if confidence < cfg.min_confidence:
            continue
        probs = win_draw_loss_probs(elo_h, elo_a, cfg.nu)

        actual = "draw" if m.home_score == m.away_score else ("home" if m.home_score > m.away_score else "away")

        # best price per selection across books
        best: dict[str, OddsSnapshot] = {}
        for (_, sel, _line), s in last.items():
            if sel not in SEL_IDX:
                continue
            if sel not in best or s.decimal_odds > best[sel].decimal_odds:
                best[sel] = s

        for sel, s in best.items():
            p = probs[SEL_IDX[sel]]
            if not (cfg.min_decimal <= s.decimal_odds <= cfg.max_decimal):
                continue
            ev = odds_math.expected_value(p, s.decimal_odds)
            if ev * 100 < cfg.min_ev_pct:
                continue

            if cfg.stake_mode == "kelly":
                stake = odds_math.kelly_fraction(p, s.decimal_odds) * cfg.kelly_fraction * bankroll
            else:
                stake = cfg.flat_stake
            stake = round(min(stake, bankroll), 2)
            if stake <= 0:
                continue

            won = sel == actual
            profit = round(stake * (s.decimal_odds - 1), 2) if won else -stake
            bankroll = round(bankroll + profit, 2)
            peak = max(peak, bankroll)
            max_dd = max(max_dd, (peak - bankroll) / peak if peak > 0 else 0.0)
            if won:
                losing_streak = 0
            else:
                losing_streak += 1
                longest_losing = max(longest_losing, losing_streak)

            bets.append({
                "match_id": m.id, "t": m.start_time.isoformat(),
                "match": f"{m.home_player.name} vs {m.away_player.name}",
                "sportsbook": s.sportsbook, "selection": sel,
                "decimal_odds": s.decimal_odds, "model_prob": round(p, 4),
                "ev_pct": round(ev * 100, 2), "stake": stake,
                "won": won, "profit": profit, "bankroll": bankroll,
            })
            curve.append({"t": m.start_time.isoformat(), "bankroll": bankroll})
            if bankroll <= 0:
                break
        if bankroll <= 0:
            break

    total = len(bets)
    wins = sum(1 for b in bets if b["won"])
    staked = sum(b["stake"] for b in bets)
    profit = round(bankroll - cfg.starting_bankroll, 2)

    def bucket(key_fn):
        agg: dict[str, dict] = {}
        for b in bets:
            k = key_fn(b)
            a = agg.setdefault(k, {"bets": 0, "profit": 0.0})
            a["bets"] += 1
            a["profit"] = round(a["profit"] + b["profit"], 2)
        return agg

    def odds_bucket(b):
        d = b["decimal_odds"]
        if d < 1.5: return "<1.50"
        if d < 2.0: return "1.50-1.99"
        if d < 3.0: return "2.00-2.99"
        return "3.00+"

    return {
        "total_bets": total, "wins": wins, "losses": total - wins, "pushes": 0,
        "profit": profit,
        "roi_pct": round(profit / staked * 100, 2) if staked else 0.0,
        "final_bankroll": bankroll,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "longest_losing_streak": longest_losing,
        "avg_decimal_odds": round(sum(b["decimal_odds"] for b in bets) / total, 3) if total else None,
        "profit_by_selection": bucket(lambda b: b["selection"]),
        "profit_by_player": bucket(lambda b: b["match"].split(" vs ")[0] if b["selection"] == "home"
                                   else (b["match"].split(" vs ")[1] if b["selection"] == "away" else "draws")),
        "profit_by_odds_range": bucket(odds_bucket),
        "curve": curve,
        "bets": bets[-500:],  # cap payload
    }
