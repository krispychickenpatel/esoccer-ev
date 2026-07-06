"""Friend Pick Ledger API (v0.3.6 Module 1)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import friend_picks as fp
from ..models import FriendPick

router = APIRouter(prefix="/api/friend-picks", tags=["friend-picks"])


class FriendPickIn(BaseModel):
    pick_side: str  # home/away/draw
    home_name: str
    away_name: str
    odds_at_pick_decimal: float
    odds_at_pick_american: int | None = None
    book_seen: str = ""
    league: str = ""
    kickoff_time: datetime | None = None
    pick_timestamp: datetime | None = None
    provider_event_id: str | None = None
    reason: str = ""
    confidence: str | None = None


@router.post("")
def create(payload: FriendPickIn, db: Session = Depends(get_db)):
    if payload.pick_side not in ("home", "away", "draw"):
        raise HTTPException(400, "pick_side must be home|away|draw")
    pick = fp.create_friend_pick(db, payload.model_dump())
    return fp.pick_out(db, pick)


@router.get("")
def list_picks(status: str | None = None, db: Session = Depends(get_db)):
    q = select(FriendPick).order_by(FriendPick.created_at.desc())
    if status:
        q = q.where(FriendPick.resolution_status == status.upper())
    rows = db.scalars(q).all()
    return {"items": [fp.pick_out(db, p) for p in rows]}


@router.get("/report")
def report(db: Session = Depends(get_db)):
    fp.score_all_resolved(db)
    return fp.report(db)


@router.get("/verify-integrity")
def verify_integrity(db: Session = Depends(get_db)):
    """Recompute every FriendPick's hash; any mismatch = tampered ledger."""
    return fp.verify_integrity(db)


@router.get("/{pick_id}")
def get_one(pick_id: int, db: Session = Depends(get_db)):
    p = db.get(FriendPick, pick_id)
    if not p:
        raise HTTPException(404, "Friend pick not found")
    return fp.pick_out(db, p)


class ResolveIn(BaseModel):
    match_id: int


@router.post("/{pick_id}/resolve")
def resolve(pick_id: int, payload: ResolveIn, db: Session = Depends(get_db)):
    pick = fp.resolve_friend_pick(db, pick_id, payload.match_id)
    if not pick:
        raise HTTPException(404, "Friend pick or match not found")
    return fp.pick_out(db, pick)


@router.post("/{pick_id}/correct")
def correct(pick_id: int, payload: dict, db: Session = Depends(get_db)):
    correction = fp.correct_friend_pick(db, pick_id, payload)
    if not correction:
        raise HTTPException(404, "Original friend pick not found")
    return fp.pick_out(db, correction)
