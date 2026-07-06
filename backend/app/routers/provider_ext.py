"""Book Coverage Scanner + Feed Shootout Prep API (v0.3.6 Modules 3-4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..engines import book_coverage, feed_comparison

router = APIRouter(prefix="/api/provider", tags=["provider-ext"])


@router.get("/bookmaker-coverage")
def bookmaker_coverage(db: Session = Depends(get_db)):
    return {"reference_feed": book_coverage.REFERENCE_BOOK, "books": book_coverage.list_coverage(db)}


@router.post("/bookmaker-coverage/scan")
def bookmaker_coverage_scan(db: Session = Depends(get_db)):
    from ..connectors.betsapi_provider import BetsApiProvider
    provider = BetsApiProvider(db)
    return book_coverage.run_scan(db, provider)


@router.get("/feed-comparison")
def feed_comparison_list(db: Session = Depends(get_db)):
    return {"items": feed_comparison.list_candidates(db)}


@router.post("/feed-comparison/manual-note")
def feed_comparison_manual_note(payload: dict, db: Session = Depends(get_db)):
    provider_name = payload.get("provider_name")
    if not provider_name:
        from fastapi import HTTPException
        raise HTTPException(400, "provider_name is required")
    fields = {k: v for k, v in payload.items() if k != "provider_name"}
    row = feed_comparison.add_manual_note(db, provider_name, **fields)
    return {"provider_name": row.provider_name, "status": row.status}
