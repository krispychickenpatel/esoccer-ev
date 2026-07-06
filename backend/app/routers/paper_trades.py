"""Paper Trade Engine API (v0.3.6 Module 6). No real betting."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import paper_trade as pt
from ..models import PaperTrade, PredictionLedger

router = APIRouter(prefix="/api/paper-trades", tags=["paper-trades"])


@router.get("")
def list_trades(signal_source: str | None = None, db: Session = Depends(get_db)):
    q = select(PaperTrade).order_by(PaperTrade.created_at.desc()).limit(1000)
    if signal_source:
        q = q.where(PaperTrade.signal_source == signal_source.upper())
    rows = db.scalars(q).all()
    return {"disclaimer": pt.DISCLAIMER, "items": [{
        "id": r.id, "signal_source": r.signal_source, "signal_id": r.signal_id,
        "match_id": r.match_id, "sportsbook": r.sportsbook, "market": r.market,
        "selection": r.selection, "signal_time": r.signal_time.isoformat(),
        "delay_seconds": r.delay_seconds, "price_decimal": r.price_decimal,
        "max_entry_decimal": r.max_entry_decimal, "entry_survived": r.entry_survived,
        "paper_stake": r.paper_stake, "paper_pl_usd": r.paper_pl_usd,
        "proxy_clv_pct": r.proxy_clv_pct, "settlement_status": r.settlement_status,
        "book_availability": r.book_availability, "feed_lag_caveat": r.feed_lag_caveat,
    } for r in rows]}


@router.get("/report")
def report(db: Session = Depends(get_db)):
    pt.resettle_all(db)
    return pt.report(db)


class SimulateIn(BaseModel):
    signal_source: str  # MODEL or FRIEND
    signal_id: int


@router.post("/simulate")
def simulate(payload: SimulateIn, db: Session = Depends(get_db)):
    src = payload.signal_source.upper()
    if src == "MODEL":
        trades = pt.simulate_model_candidate(db, payload.signal_id)
        if trades is None:
            raise HTTPException(404, "PredictionLedger row not found, or action != BET")
    elif src == "FRIEND":
        trades = pt.simulate_friend_pick(db, payload.signal_id)
        if trades is None:
            raise HTTPException(404, "Friend pick not found, or not resolved to a match")
    else:
        raise HTTPException(400, "signal_source must be MODEL or FRIEND")
    return {"disclaimer": pt.DISCLAIMER, "created": len(trades),
            "trades": [{"delay_seconds": t.delay_seconds, "settlement_status": t.settlement_status,
                       "price_decimal": t.price_decimal, "entry_survived": t.entry_survived}
                      for t in trades]}


@router.post("/simulate-all")
def simulate_all(db: Session = Depends(get_db)):
    """Bulk convenience: simulate every BET model prediction and every
    RESOLVED friend pick that hasn't been simulated yet."""
    from ..models import FriendPick
    created_model = 0
    for pred in db.scalars(select(PredictionLedger).where(PredictionLedger.action == "BET")).all():
        existing = db.scalar(select(PaperTrade).where(
            PaperTrade.signal_source == "MODEL", PaperTrade.signal_id == pred.id))
        if existing is None:
            pt.simulate_model_candidate(db, pred.id)
            created_model += 1
    created_friend = 0
    for pick in db.scalars(select(FriendPick).where(FriendPick.resolution_status == "RESOLVED")).all():
        existing = db.scalar(select(PaperTrade).where(
            PaperTrade.signal_source == "FRIEND", PaperTrade.signal_id == pick.id))
        if existing is None:
            pt.simulate_friend_pick(db, pick.id)
            created_friend += 1
    pt.resettle_all(db)
    return {"disclaimer": pt.DISCLAIMER, "model_signals_simulated": created_model,
            "friend_signals_simulated": created_friend}
