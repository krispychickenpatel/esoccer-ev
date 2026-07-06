"""Feed Shootout Prep (v0.3.6 Module 4).

Storage + manual-note framework for comparing BetsAPI against other odds
feeds later. No paid integrations are wired in this version -- this is
schema and endpoints only, seeded with one row of real evidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FeedCandidate

BETSAPI_FIRST_LIVE_LATENCY_NOTE = (
    "~20-30s observed floor (avg 26-29s, median 20-25s, p95 64-84s; n=30 "
    "across two clean validation sessions, max_matches=5 and max_matches=2). "
    "Cutting tracked load did not meaningfully change the typical case -- "
    "the delay is provider-side, not poller-side. See "
    "notes/fixed-max2-first-live-validation-report.md and "
    "notes/v0.3.5-provider-execution-fix-report.md."
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_seeded(db: Session) -> None:
    """Idempotent: seed exactly one real-evidence row for BetsAPI/bet365 if
    it doesn't already exist. Never overwrites a row that's been edited."""
    existing = db.scalar(select(FeedCandidate).where(FeedCandidate.provider_name == "BetsAPI/bet365"))
    if existing is not None:
        return
    db.add(FeedCandidate(
        provider_name="BetsAPI/bet365",
        supported_leagues_json=json.dumps([
            "Esoccer Battle - 8 mins play", "Esoccer H2H GG League - 8 mins play",
            "Esoccer GT Leagues – 12 mins play", "Esoccer Adriatic League - 10 mins play",
            "Esoccer Battle Volta - 6 mins play",
        ]),
        supported_markets_json=json.dumps(["ML_3WAY", "SPREAD_2WAY"]),
        supported_books_json=json.dumps(["bet365"]),
        first_live_latency_note=BETSAPI_FIRST_LIVE_LATENCY_NOTE,
        timestamp_quality="add_time epoch seconds per tick, no sub-second precision",
        raw_payload_availability=True,
        cost_notes="Existing subscription already in use; no incremental cost for current volume.",
        status="WORKS",
        updated_at=_now(),
    ))
    db.commit()


def list_candidates(db: Session) -> list[dict]:
    ensure_seeded(db)
    rows = db.scalars(select(FeedCandidate).order_by(FeedCandidate.provider_name)).all()
    return [{
        "id": r.id, "provider_name": r.provider_name,
        "supported_leagues": json.loads(r.supported_leagues_json or "[]"),
        "supported_markets": json.loads(r.supported_markets_json or "[]"),
        "supported_books": json.loads(r.supported_books_json or "[]"),
        "first_live_latency_note": r.first_live_latency_note,
        "timestamp_quality": r.timestamp_quality,
        "raw_payload_availability": r.raw_payload_availability,
        "cost_notes": r.cost_notes, "status": r.status,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]


def add_manual_note(db: Session, provider_name: str, **fields) -> FeedCandidate:
    row = db.scalar(select(FeedCandidate).where(FeedCandidate.provider_name == provider_name))
    row = row or FeedCandidate(provider_name=provider_name, status="CANDIDATE")
    for key in ("supported_leagues", "supported_markets", "supported_books"):
        if key in fields and fields[key] is not None:
            setattr(row, f"{key}_json", json.dumps(fields[key]))
    for key in ("first_live_latency_note", "timestamp_quality", "cost_notes", "status",
               "raw_payload_availability"):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])
    row.updated_at = _now()
    if row.id is None:
        db.add(row)
    db.commit()
    return row
