from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Alert, Bet, Match, OddsSnapshot, Prediction, Settings

router = APIRouter(prefix="/api", tags=["admin"])


SEED_SOURCES = ("manual_seed", "synthetic_demo", "seed")


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), include_seed: bool | None = None):
    s = db.get(Settings, 1)
    if include_seed is None:
        include_seed = bool(s.include_seed_data)
    all_bets = db.scalars(select(Bet).order_by(Bet.placed_at)).all()
    seed_bets = [b for b in all_bets if b.data_source in SEED_SOURCES]
    verified_bets = [b for b in all_bets if b.data_source not in SEED_SOURCES]
    bets = all_bets if include_seed else verified_bets
    settled = [b for b in bets if b.result in ("win", "loss", "push")]
    wins = [b for b in settled if b.result == "win"]
    decided = [b for b in settled if b.result in ("win", "loss")]
    staked = sum(b.stake for b in settled)
    profit = round(sum(b.profit for b in settled), 2)
    roi = profit / staked * 100 if staked else 0.0

    # 95% CI half-width on ROI, normal approx on per-bet returns — a reminder
    # of how wide the noise band is at small n.
    roi_ci = None
    if len(settled) >= 2 and staked:
        rets = [b.profit / b.stake for b in settled if b.stake]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        roi_ci = round(1.96 * math.sqrt(var / len(rets)) * 100, 1)

    by_market: dict[str, dict] = {}
    for b in settled:
        a = by_market.setdefault(b.market, {"bets": 0, "profit": 0.0})
        a["bets"] += 1
        a["profit"] = round(a["profit"] + b.profit, 2)
    ranked = sorted(by_market.items(), key=lambda kv: kv[1]["profit"])

    clvs = [b.clv_pct for b in bets if b.clv_pct is not None]

    # Model accuracy on finished matches that have predictions
    preds = db.scalars(select(Prediction)).all()
    brier_terms, hits, graded = [], 0, 0
    for p in preds:
        m = db.get(Match, p.match_id)
        if not m or m.winner not in ("home", "draw", "away"):
            continue
        actual = {"home": (1, 0, 0), "draw": (0, 1, 0), "away": (0, 0, 1)}[m.winner]
        vec = (p.p_home, p.p_draw, p.p_away)
        brier_terms.append(sum((vec[i] - actual[i]) ** 2 for i in range(3)))
        picked = ("home", "draw", "away")[vec.index(max(vec))]
        hits += picked == m.winner
        graded += 1

    open_alerts = db.scalars(select(Alert).where(Alert.status == "open")).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    curve, bank = [], s.starting_bankroll
    for b in settled:
        bank = round(bank + b.profit, 2)
        curve.append({"t": b.placed_at.isoformat(), "bankroll": bank})

    def window_loss(days: int) -> float:
        cutoff = now - timedelta(days=days)
        return round(sum(b.profit for b in settled if b.placed_at >= cutoff), 2)

    
    def _mini(bs):
        st = [b for b in bs if b.result in ("win", "loss", "push")]
        stk = sum(b.stake for b in st)
        pf = round(sum(b.profit for b in st), 2)
        return {"settled": len(st), "profit": pf,
                "roi_pct": round(pf / stk * 100, 2) if stk else None}

    seed_split = {"verified": _mini(verified_bets), "seed": _mini(seed_bets),
                  "include_seed": include_seed,
                  "seed_warning": ("Metrics include SEED/SAMPLE rows"
                                   if include_seed and seed_bets else None)}

    return {
        "seed_split": seed_split,
        "bankroll": round(s.starting_bankroll + profit, 2),
        "starting_bankroll": s.starting_bankroll,
        "profit": profit, "roi_pct": round(roi, 2), "roi_ci95_pct": roi_ci,
        "win_rate": round(len(wins) / len(decided) * 100, 1) if decided else None,
        "total_bets": len(bets), "settled_bets": len(settled),
        "avg_american_odds": round(sum(b.american_odds for b in settled) / len(settled)) if settled else None,
        "avg_decimal_odds": round(sum(b.decimal_odds for b in settled) / len(settled), 3) if settled else None,
        "best_markets": [{"market": k, **v} for k, v in ranked[::-1][:3]],
        "worst_markets": [{"market": k, **v} for k, v in ranked[:3]],
        "recent_bets": [{
            "id": b.id, "placed_at": b.placed_at.isoformat(), "match_label": b.match_label,
            "selection": b.selection, "american_odds": b.american_odds,
            "stake": b.stake, "result": b.result, "profit": b.profit,
        } for b in sorted(bets, key=lambda x: x.placed_at, reverse=True)[:8]],
        "open_alerts": len(open_alerts),
        "avg_clv_pct": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "clv_sample": len(clvs),
        "model": {
            "graded_predictions": graded,
            "brier": round(sum(brier_terms) / graded, 4) if graded else None,
            "hit_rate_pct": round(hits / graded * 100, 1) if graded else None,
        },
        "risk": {
            "pnl_1d": window_loss(1), "pnl_7d": window_loss(7),
            "max_daily_loss": s.max_daily_loss, "max_weekly_loss": s.max_weekly_loss,
            "daily_breached": window_loss(1) <= -s.max_daily_loss,
            "weekly_breached": window_loss(7) <= -s.max_weekly_loss,
        },
        "bankroll_curve": curve,
    }


class SettingsIn(BaseModel):
    starting_bankroll: float
    unit_size: float
    max_bet_size: float
    min_ev_pct: float
    kelly_fraction: float
    max_daily_loss: float
    max_weekly_loss: float
    max_drawdown_shutdown_pct: float
    sportsbooks_tracked: list[str]
    markets_tracked: list[str]
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    include_seed_data: bool = False
    exec_window_seconds: int = 30
    poller_enabled: bool = False
    min_verified_history: int = 20
    min_similar_sample: int = 8
    tracked_leagues: list[str] = []
    validation_mode_enabled: bool = False
    validation_max_matches: int = 5


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    return {
        "starting_bankroll": s.starting_bankroll, "unit_size": s.unit_size,
        "max_bet_size": s.max_bet_size, "min_ev_pct": s.min_ev_pct,
        "kelly_fraction": s.kelly_fraction, "max_daily_loss": s.max_daily_loss,
        "max_weekly_loss": s.max_weekly_loss,
        "max_drawdown_shutdown_pct": s.max_drawdown_shutdown_pct,
        "sportsbooks_tracked": json.loads(s.sportsbooks_tracked),
        "markets_tracked": json.loads(s.markets_tracked),
        "discord_webhook_url": s.discord_webhook_url,
        "telegram_bot_token": s.telegram_bot_token,
        "telegram_chat_id": s.telegram_chat_id,
        "include_seed_data": s.include_seed_data,
        "exec_window_seconds": s.exec_window_seconds,
        "poller_enabled": s.poller_enabled,
        "min_verified_history": s.min_verified_history,
        "min_similar_sample": s.min_similar_sample,
        "tracked_leagues": json.loads(s.tracked_leagues or "[]"),
        "validation_mode_enabled": bool(s.validation_mode_enabled),
        "validation_max_matches": s.validation_max_matches,
    }


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db)):
    s = db.get(Settings, 1)
    d = payload.model_dump()
    d["sportsbooks_tracked"] = json.dumps(d["sportsbooks_tracked"])
    d["markets_tracked"] = json.dumps(d["markets_tracked"])
    d["tracked_leagues"] = json.dumps(d["tracked_leagues"])
    for k, v in d.items():
        setattr(s, k, v)
    db.commit()
    return get_settings(db)


def _csv_response(header: list[str], rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/bets.csv")
def export_bets(db: Session = Depends(get_db)):
    rows = db.scalars(select(Bet).order_by(Bet.placed_at)).all()
    return _csv_response(
        ["placed_at", "sportsbook", "league", "match_label", "selection", "opponent", "market",
         "line", "american_odds", "decimal_odds", "stake", "result", "payout", "profit",
         "closing_american_odds", "clv_pct", "model_prob", "ev_at_placement", "notes"],
        [[b.placed_at.isoformat(), b.sportsbook, b.league, b.match_label, b.selection, b.opponent,
          b.market, b.line, b.american_odds, b.decimal_odds, b.stake, b.result, b.payout,
          b.profit, b.closing_american_odds, b.clv_pct, b.model_prob, b.ev_at_placement, b.notes]
         for b in rows], "bet_history_export.csv")


@router.get("/export/matches.csv")
def export_matches(db: Session = Depends(get_db)):
    rows = db.scalars(select(Match).order_by(Match.start_time)).all()
    return _csv_response(
        ["ext_id", "start_time", "league", "home_player", "away_player",
         "home_score", "away_score", "ht_home_score", "ht_away_score", "duration_min"],
        [[m.ext_id, m.start_time.isoformat(), m.league, m.home_player.name, m.away_player.name,
          m.home_score, m.away_score, m.ht_home_score, m.ht_away_score, m.duration_min]
         for m in rows], "match_results_export.csv")


@router.get("/export/odds.csv")
def export_odds(db: Session = Depends(get_db)):
    rows = db.scalars(select(OddsSnapshot).order_by(OddsSnapshot.collected_at)).all()
    ext = {m.id: m.ext_id for m in db.scalars(select(Match)).all()}
    return _csv_response(
        ["ext_id", "sportsbook", "market", "selection", "line", "american_odds",
         "collected_at", "is_opening", "is_closing"],
        [[ext.get(r.match_id), r.sportsbook, r.market, r.selection, r.line, r.american_odds,
          r.collected_at.isoformat(), r.is_opening, r.is_closing] for r in rows],
        "odds_snapshots_export.csv")


@router.get("/export/predictions.csv")
def export_predictions(db: Session = Depends(get_db)):
    rows = db.scalars(select(Prediction).order_by(Prediction.created_at)).all()
    return _csv_response(
        ["match_id", "model", "p_home", "p_draw", "p_away",
         "fair_home", "fair_draw", "fair_away", "confidence", "created_at"],
        [[r.match_id, r.model, r.p_home, r.p_draw, r.p_away,
          r.fair_home, r.fair_draw, r.fair_away, r.confidence, r.created_at.isoformat()]
         for r in rows], "predictions_export.csv")

@router.post("/admin/real-mode-clean")
def real_mode_clean(db: Session = Depends(get_db)):
    """Remove all demo/seed operational rows so the app stops showing bogus
    $10 demo bets or screenshot-derived seed picks as if they were live data.
    Keeps verified CSV/API rows. Safe to run repeatedly."""
    from ..models import ExecutionLog, MarketEvent, Pick, Recommendation

    seed_sources = ("manual_seed", "synthetic_demo", "seed")
    counts = {"bets": 0, "matches": 0, "odds": 0, "recommendations": 0,
              "executions": 0, "picks": 0, "market_events": 0}

    seed_match_ids = [m.id for m in db.scalars(select(Match).where(
        Match.source.in_(seed_sources))).all()]

    pick_q = select(Pick).where(Pick.include_in_metrics.is_(False))
    if seed_match_ids:
        pick_q = select(Pick).where((Pick.include_in_metrics.is_(False)) |
                                    (Pick.match_id.in_(seed_match_ids)))
    for p in db.scalars(pick_q).all():
        db.delete(p); counts["picks"] += 1

    for e in db.scalars(select(ExecutionLog).where(ExecutionLog.data_source.in_(seed_sources))).all():
        db.delete(e); counts["executions"] += 1
    for r in db.scalars(select(Recommendation).where(Recommendation.data_source.in_(seed_sources))).all():
        db.delete(r); counts["recommendations"] += 1

    odds_q = select(OddsSnapshot).where(OddsSnapshot.data_source.in_(seed_sources))
    if seed_match_ids:
        odds_q = select(OddsSnapshot).where((OddsSnapshot.data_source.in_(seed_sources)) |
                                            (OddsSnapshot.match_id.in_(seed_match_ids)))
    for o in db.scalars(odds_q).all():
        db.delete(o); counts["odds"] += 1

    if seed_match_ids:
        for me in db.scalars(select(MarketEvent).where(MarketEvent.match_id.in_(seed_match_ids))).all():
            db.delete(me); counts["market_events"] += 1

    for b in db.scalars(select(Bet).where(Bet.data_source.in_(seed_sources))).all():
        db.delete(b); counts["bets"] += 1
    for m in db.scalars(select(Match).where(Match.source.in_(seed_sources))).all():
        db.delete(m); counts["matches"] += 1

    s = db.get(Settings, 1)
    if s:
        s.include_seed_data = False
    db.commit()
    return {"ok": True, "removed": counts, "include_seed_data": False,
            "note": "Real mode enabled: seed/demo rows removed from operational views."}
