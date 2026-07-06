from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines.backtest import BacktestConfig, run_backtest
from ..engines.predict import predict_match, scan_ev_opportunities
from ..models import Alert, BacktestRun, Match, Prediction, Settings
from ..webhooks import format_alert, send_discord, send_telegram

router = APIRouter(prefix="/api", tags=["engine"])


@router.get("/predictions")
def list_predictions(db: Session = Depends(get_db), limit: int = 200):
    rows = db.scalars(select(Prediction).order_by(Prediction.created_at.desc()).limit(limit)).all()
    match_ids = {r.match_id for r in rows}
    matches = {m.id: m for m in db.scalars(select(Match).where(Match.id.in_(match_ids))).all()} if match_ids else {}
    out = []
    for r in rows:
        m = matches.get(r.match_id)
        out.append({
            "id": r.id, "match_id": r.match_id,
            "match": f"{m.home_player.name} vs {m.away_player.name}" if m else "",
            "start_time": m.start_time.isoformat() if m else None,
            "finished": bool(m and m.home_score is not None),
            "actual": m.winner if m else None,
            "model": r.model, "p_home": r.p_home, "p_draw": r.p_draw, "p_away": r.p_away,
            "fair_home": r.fair_home, "fair_draw": r.fair_draw, "fair_away": r.fair_away,
            "confidence": r.confidence, "features": json.loads(r.features_json),
            "created_at": r.created_at.isoformat(),
        })
    return out


@router.post("/predictions/generate")
def generate_predictions(db: Session = Depends(get_db)):
    """Predict all upcoming (unfinished, future) matches from current ratings."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    upcoming = db.scalars(select(Match).where(
        Match.home_score.is_(None), Match.start_time > now)).all()
    made = [predict_match(db, m).id for m in upcoming]
    return {"generated": len(made)}


@router.get("/ev/opportunities")
def ev_opportunities(db: Session = Depends(get_db)):
    return scan_ev_opportunities(db, create_alerts=False)


@router.post("/alerts/scan")
def alerts_scan(db: Session = Depends(get_db), notify: bool = False):
    opps = scan_ev_opportunities(db, create_alerts=True)
    sent = 0
    if notify and opps:
        s = db.get(Settings, 1)
        for o in opps[:10]:
            text = format_alert(o)
            sent += send_discord(s.discord_webhook_url, text)
            sent += send_telegram(s.telegram_bot_token, s.telegram_chat_id, text)
    return {"opportunities": len(opps), "notifications_sent": sent}


@router.get("/alerts")
def list_alerts(db: Session = Depends(get_db), status: str = "open"):
    q = select(Alert).order_by(Alert.created_at.desc()).limit(200)
    if status != "all":
        q = q.where(Alert.status == status)
    rows = db.scalars(q).all()
    match_ids = {r.match_id for r in rows}
    matches = {m.id: m for m in db.scalars(select(Match).where(Match.id.in_(match_ids))).all()} if match_ids else {}
    return [{
        "id": a.id, "match_id": a.match_id,
        "match": (f"{matches[a.match_id].home_player.name} vs "
                  f"{matches[a.match_id].away_player.name}") if a.match_id in matches else "",
        "start_time": matches[a.match_id].start_time.isoformat() if a.match_id in matches else None,
        "market": a.market, "selection": a.selection, "line": a.line,
        "sportsbook": a.sportsbook, "book_american": a.book_american,
        "book_decimal": a.book_decimal, "model_prob": a.model_prob,
        "fair_decimal": a.fair_decimal, "ev_pct": a.ev_pct,
        "suggested_stake": a.suggested_stake, "reason": a.reason,
        "status": a.status, "created_at": a.created_at.isoformat(),
    } for a in rows]


@router.put("/alerts/{alert_id}/status")
def set_alert_status(alert_id: int, status: str, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    if status not in ("open", "expired", "taken", "dismissed"):
        raise HTTPException(400, "Bad status")
    a.status = status
    db.commit()
    return {"id": a.id, "status": a.status}


class BacktestIn(BaseModel):
    name: str = ""
    date_from: datetime | None = None
    date_to: datetime | None = None
    market: str = "ML_3WAY"
    min_ev_pct: float = 5.0
    min_confidence: float = 0.0
    player_ids: list[int] = []
    min_decimal: float = 1.01
    max_decimal: float = 100.0
    stake_mode: str = "flat"
    flat_stake: float = 10.0
    kelly_fraction: float = 0.25
    starting_bankroll: float = 1000.0
    nu: float = 0.63


@router.post("/backtests")
def create_backtest(payload: BacktestIn, db: Session = Depends(get_db)):
    cfg = BacktestConfig(**payload.model_dump(exclude={"name"}))
    result = run_backtest(db, cfg)
    run = BacktestRun(
        name=payload.name or f"bt-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        config_json=payload.model_dump_json(),
        results_json=json.dumps({k: v for k, v in result.items() if k not in ("curve", "bets")}),
        curve_json=json.dumps(result["curve"]))
    db.add(run)
    db.commit()
    result["run_id"] = run.id
    return result


@router.get("/backtests")
def list_backtests(db: Session = Depends(get_db)):
    rows = db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(50)).all()
    return [{
        "id": r.id, "name": r.name, "created_at": r.created_at.isoformat(),
        "config": json.loads(r.config_json), "results": json.loads(r.results_json),
        "curve": json.loads(r.curve_json),
    } for r in rows]
