"""Shared reference-feed pricing lookup (v0.3.6 Provider Execution Fix).

Single source of truth for "what price was available at signal_time + delay"
-- used by both the Paper Trade Engine and Friend Pick scoring so the two
never disagree about a fill. Never fabricates a price: if no snapshot exists
within STALE_AFTER_SECONDS of the target time, the caller gets STALE_UNKNOWN
and must treat it as a missed price, not interpolate one.

CRITICAL CONTEXT: every price this function returns came from a reference
feed (BetsAPI/bet365) with an observed ~20-30s live-odds publication lag
(see notes/fixed-max2-first-live-validation-report.md). A price "at time T"
may actually have been posted by the book up to ~20-30s before T. Treat every
result as optimistic, not proof of a real fill.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import OddsSnapshot

STALE_AFTER_SECONDS = 60


def price_at_delay(db: Session, match_id: int, sportsbook: str, market: str,
                    selection: str, signal_time: datetime, delay_seconds: int,
                    line: float | None = None) -> tuple[int | None, float | None, str]:
    """Returns (snapshot_id, decimal_odds, status). status is "OK" or
    "STALE_UNKNOWN". Uses the latest snapshot with collected_at <= target."""
    target = signal_time + timedelta(seconds=delay_seconds)
    q = select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market,
        OddsSnapshot.selection == selection,
        OddsSnapshot.collected_at <= target,
    ).order_by(OddsSnapshot.collected_at.desc())
    if line is not None:
        q = q.where(OddsSnapshot.line == line)
    snap = db.scalars(q).first()
    if snap is None:
        return None, None, "STALE_UNKNOWN"
    if (target - snap.collected_at).total_seconds() > STALE_AFTER_SECONDS:
        return None, None, "STALE_UNKNOWN"
    return snap.id, snap.decimal_odds, "OK"


def latest_snapshot_for(db: Session, match_id: int, sportsbook: str, market: str,
                        selection: str, line: float | None = None) -> OddsSnapshot | None:
    """Latest snapshot regardless of time -- used as a proxy-closing-line
    reference. Not a true closing price (see proxy_clv naming everywhere)."""
    q = select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market,
        OddsSnapshot.selection == selection,
    ).order_by(OddsSnapshot.collected_at.desc())
    if line is not None:
        q = q.where(OddsSnapshot.line == line)
    return db.scalars(q).first()
