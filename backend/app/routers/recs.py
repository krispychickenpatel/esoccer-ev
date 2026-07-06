"""Recommendations + Execution Log + Seed Review (Evidence Notes live here, D12)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import csv_v2
from ..database import get_db
from ..engines.identity import canonical_name, resolve_player
from ..models import ExecutionLog, Match, Recommendation, Settings

router = APIRouter(prefix="/api", tags=["recommendations"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _link_match(db: Session, r: Recommendation) -> None:
    """Attach a rec to a Match by canonical players + scheduled_start (±15 min)."""
    if r.match_id or not r.scheduled_start:
        return
    hp = resolve_player(db, r.home_name, league=r.league,
                        data_source=r.data_source, verification_status=r.verification_status)
    ap = resolve_player(db, r.away_name, league=r.league,
                        data_source=r.data_source, verification_status=r.verification_status)
    if not hp or not ap:
        return
    for m in db.scalars(select(Match).where(
            Match.home_player_id.in_((hp.id, ap.id)),
            Match.away_player_id.in_((hp.id, ap.id)))).all():
        if abs((m.start_time - r.scheduled_start).total_seconds()) <= 900:
            r.match_id = m.id
            return


def _rec_out(db: Session, r: Recommendation) -> dict:
    ex = db.scalars(select(ExecutionLog).where(
        ExecutionLog.recommendation_id == r.id)).all()
    return {
        "id": r.id, "ext_id": r.ext_id, "source_name": r.source_name,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "scheduled_start": r.scheduled_start.isoformat() if r.scheduled_start else None,
        "league": r.league, "home_name": r.home_name, "away_name": r.away_name,
        "match_id": r.match_id,
        "recommended_selection": r.recommended_selection,
        "canonical_selection": canonical_name(r.recommended_selection),
        "acceptable_markets": json.loads(r.acceptable_markets or "[]"),
        "max_spread": r.max_spread, "min_american_odds": r.min_american_odds,
        "ideal_american_odds": r.ideal_american_odds,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "confidence_label": r.confidence_label, "stake_plan": r.stake_plan,
        "sportsbook": r.sportsbook, "limit_seen": r.limit_seen,
        "status": r.status, "notes": r.notes, "screenshot_ref": r.screenshot_ref,
        "data_source": r.data_source, "verification_status": r.verification_status,
        "lead_time_min": (round((r.scheduled_start - r.received_at).total_seconds() / 60, 1)
                          if r.scheduled_start and r.received_at else None),
        "executions": [_exec_out(e) for e in ex],
    }


def _exec_out(e: ExecutionLog) -> dict:
    return {
        "id": e.id, "ext_id": e.ext_id, "recommendation_id": e.recommendation_id,
        "sportsbook": e.sportsbook,
        "opened_at": e.opened_at.isoformat() if e.opened_at else None,
        "live_detected_at": e.live_detected_at.isoformat() if e.live_detected_at else None,
        "bet_placed_at": e.bet_placed_at.isoformat() if e.bet_placed_at else None,
        "actual_market": e.actual_market, "actual_line": e.actual_line,
        "actual_american_odds": e.actual_american_odds,
        "odds_at_slip": e.odds_at_slip, "odds_at_first_live": e.odds_at_first_live,
        "odds_move_slip_to_live": (e.odds_at_first_live - e.odds_at_slip
                                   if e.odds_at_first_live is not None and e.odds_at_slip is not None else None),
        "odds_move_live_to_bet": (e.actual_american_odds - e.odds_at_first_live
                                  if e.actual_american_odds is not None and e.odds_at_first_live is not None else None),
        "stake": e.stake, "accepted_odds_movement": e.accepted_odds_movement,
        "was_within_window": e.was_within_window, "latency_seconds": e.latency_seconds,
        "status": e.status, "missed_reason": e.missed_reason, "notes": e.notes,
        "data_source": e.data_source, "verification_status": e.verification_status,
    }


@router.get("/recommendations")
def list_recs(db: Session = Depends(get_db), status: str = "all", source: str = "all"):
    q = select(Recommendation).order_by(Recommendation.scheduled_start.desc().nullslast())
    rows = db.scalars(q).all()
    if status != "all":
        rows = [r for r in rows if r.status == status]
    if source != "all":
        rows = [r for r in rows if r.source_name == source]
    return [_rec_out(db, r) for r in rows]


class RecIn(BaseModel):
    source_name: str = "friend"
    received_at: datetime | None = None
    scheduled_start: datetime | None = None
    league: str = ""
    home_name: str = ""
    away_name: str = ""
    recommended_selection: str = ""
    acceptable_markets: list[str] = ["ML_3WAY"]
    max_spread: float | None = None
    min_american_odds: int | None = None
    ideal_american_odds: int | None = None
    expires_at: datetime | None = None
    confidence_label: str = "medium"
    stake_plan: float | None = None
    sportsbook: str = ""
    limit_seen: float | None = None
    notes: str = ""
    screenshot_ref: str | None = None
    status: str | None = None
    verification_status: str | None = None


@router.post("/recommendations")
def create_rec(payload: RecIn, db: Session = Depends(get_db)):
    r = Recommendation(**{**payload.model_dump(exclude={"acceptable_markets", "status",
                                                        "verification_status"}),
                          "acceptable_markets": json.dumps(payload.acceptable_markets)})
    if payload.received_at is None:
        r.received_at = _now()
    if payload.expires_at is None and r.scheduled_start:
        from datetime import timedelta
        st = db.get(Settings, 1)
        r.expires_at = r.scheduled_start + timedelta(seconds=st.exec_window_seconds if st else 30)
    _link_match(db, r)
    db.add(r)
    db.commit()
    return _rec_out(db, r)


@router.put("/recommendations/{rec_id}")
def update_rec(rec_id: int, payload: RecIn, db: Session = Depends(get_db)):
    r = db.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404, "Recommendation not found")
    data = payload.model_dump(exclude_unset=True)
    if "acceptable_markets" in data:
        data["acceptable_markets"] = json.dumps(data["acceptable_markets"])
    for k, v in data.items():
        setattr(r, k, v)
    if r.data_source == "manual_seed" and payload.verification_status is None:
        r.verification_status = "user_verified"  # editing a seed row = user reviewed it
    _link_match(db, r)
    db.commit()
    return _rec_out(db, r)


@router.delete("/recommendations/{rec_id}")
def delete_rec(rec_id: int, db: Session = Depends(get_db)):
    r = db.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404, "Recommendation not found")
    for e in db.scalars(select(ExecutionLog).where(
            ExecutionLog.recommendation_id == rec_id)).all():
        db.delete(e)
    db.delete(r)
    db.commit()
    return {"deleted": rec_id}


@router.post("/recommendations/{rec_id}/mark")
def mark_rec(rec_id: int, status: str, db: Session = Depends(get_db)):
    r = db.get(Recommendation, rec_id)
    if not r:
        raise HTTPException(404, "Recommendation not found")
    allowed = ("pending", "live-ready", "placed", "missed", "rejected", "expired",
               "settled", "pass")
    if status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    r.status = status
    db.commit()
    return {"id": r.id, "status": r.status}


@router.post("/recommendations/import")
async def import_recs(db: Session = Depends(get_db), file: UploadFile = File(...),
                      dry_run: bool = False):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows, errors, warnings = csv_v2.parse_recommendations(text)
    report = {"parsed": len(rows), "errors": errors, "warnings": warnings,
              "dry_run": dry_run, "imported": 0, "duplicates": 0,
              "preview": [dict(r, received_at=str(r["received_at"]),
                               scheduled_start=str(r["scheduled_start"]),
                               expires_at=str(r["expires_at"])) for r in rows[:20]]}
    if dry_run or errors:
        if errors and not dry_run:
            raise HTTPException(422, detail=report)
        return report
    for r in rows:
        if r["ext_id"] and db.scalar(select(Recommendation).where(
                Recommendation.ext_id == r["ext_id"])):
            report["duplicates"] += 1
            continue
        rec = Recommendation(**{k: v for k, v in r.items() if not k.startswith("_")},
                             data_source="csv_import", verification_status="user_verified")
        _link_match(db, rec)
        db.add(rec)
        report["imported"] += 1
    db.commit()
    return report


# ------------------------------------------------------------- executions
class ExecIn(BaseModel):
    recommendation_id: int | None = None
    sportsbook: str = ""
    opened_at: datetime | None = None
    live_detected_at: datetime | None = None
    bet_placed_at: datetime | None = None
    actual_market: str = ""
    actual_line: float | None = None
    actual_american_odds: int | None = None
    odds_at_slip: int | None = None
    odds_at_first_live: int | None = None
    stake: float | None = None
    accepted_odds_movement: bool = False
    status: str = "placed"
    missed_reason: str = ""
    notes: str = ""


def _derive_exec(e: ExecutionLog, db: Session):
    if e.latency_seconds is None and e.live_detected_at and e.bet_placed_at:
        e.latency_seconds = round((e.bet_placed_at - e.live_detected_at).total_seconds(), 1)
    if e.was_within_window is None and e.recommendation_id:
        r = db.get(Recommendation, e.recommendation_id)
        if r and r.expires_at and e.bet_placed_at:
            e.was_within_window = e.bet_placed_at <= r.expires_at
        if r and e.live_detected_at and not r.first_live_seen_at:
            r.first_live_seen_at = e.live_detected_at


@router.get("/executions")
def list_execs(db: Session = Depends(get_db)):
    return [_exec_out(e) for e in db.scalars(
        select(ExecutionLog).order_by(ExecutionLog.bet_placed_at.desc().nullslast())).all()]


@router.post("/executions")
def create_exec(payload: ExecIn, db: Session = Depends(get_db)):
    e = ExecutionLog(**payload.model_dump())
    _derive_exec(e, db)
    db.add(e)
    if e.recommendation_id and e.status in ("placed", "missed", "rejected"):
        r = db.get(Recommendation, e.recommendation_id)
        if r and r.status in ("pending", "live-ready"):
            r.status = e.status
    db.commit()
    return _exec_out(e)


@router.put("/executions/{exec_id}")
def update_exec(exec_id: int, payload: ExecIn, db: Session = Depends(get_db)):
    e = db.get(ExecutionLog, exec_id)
    if not e:
        raise HTTPException(404, "Execution not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(e, k, v)
    _derive_exec(e, db)
    db.commit()
    return _exec_out(e)


@router.post("/executions/import")
async def import_execs(db: Session = Depends(get_db), file: UploadFile = File(...),
                       dry_run: bool = False):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows, errors, warnings = csv_v2.parse_executions(text)
    report = {"parsed": len(rows), "errors": errors, "warnings": warnings,
              "dry_run": dry_run, "imported": 0, "duplicates": 0, "unlinked": 0}
    if dry_run or errors:
        if errors and not dry_run:
            raise HTTPException(422, detail=report)
        return report
    for r in rows:
        if r["ext_id"] and db.scalar(select(ExecutionLog).where(
                ExecutionLog.ext_id == r["ext_id"])):
            report["duplicates"] += 1
            continue
        rec_id = None
        if r.get("rec_ext_id"):
            rec = db.scalar(select(Recommendation).where(
                Recommendation.ext_id == r["rec_ext_id"]))
            rec_id = rec.id if rec else None
            if rec is None:
                report["unlinked"] += 1
        e = ExecutionLog(**{k: v for k, v in r.items()
                            if not k.startswith("_") and k != "rec_ext_id"},
                         recommendation_id=rec_id,
                         data_source="csv_import", verification_status="user_verified")
        _derive_exec(e, db)
        db.add(e)
        report["imported"] += 1
    db.commit()
    return report


# ------------------------------------------------------------- seed review
@router.get("/seed/review")
def seed_review(db: Session = Depends(get_db)):
    """Everything still carrying seed_partial — approve/edit/delete from the UI."""
    from ..models import Bet
    recs = db.scalars(select(Recommendation).where(
        Recommendation.data_source == "manual_seed",
        Recommendation.verification_status == "seed_partial")).all()
    bets = db.scalars(select(Bet).where(
        Bet.data_source == "manual_seed",
        Bet.verification_status == "seed_partial")).all()
    return {
        "recommendations": [_rec_out(db, r) for r in recs],
        "bets": [{"id": b.id, "ext_id": b.ext_id, "placed_at": b.placed_at.isoformat(),
                  "selection": b.selection, "opponent": b.opponent, "market": b.market,
                  "line": b.line, "american_odds": b.american_odds, "stake": b.stake,
                  "result": b.result, "profit": b.profit, "notes": b.notes,
                  "screenshot_ref": b.screenshot_ref} for b in bets],
        "note": "Rows are manually reconstructed from screenshots (SEED). "
                "Approve, correct, or delete before trusting any analysis built on them.",
    }


@router.post("/seed/approve")
def seed_approve(kind: str, item_id: int, db: Session = Depends(get_db)):
    from ..models import Bet
    obj = db.get(Recommendation if kind == "recommendation" else Bet, item_id)
    if not obj:
        raise HTTPException(404, f"{kind} {item_id} not found")
    obj.verification_status = "user_verified"
    db.commit()
    return {"kind": kind, "id": item_id, "verification_status": "user_verified"}
