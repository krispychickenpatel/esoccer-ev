"""v0.3.7D.2: migration-safe, time-bounded collection-run evidence.

Fixes a v0.3.7D.1 defect: `verdict_hierarchy.determine_verdict()`'s
`collection_has_run` signal was derived solely from the new
`Settings.last_completed_run_*` bookkeeping (added in D.1). A run that
started and stopped under PRE-D.1 code leaves those fields NULL even
though real, substantial collection activity occurred -- this produced a
false COLLECTION_NOT_RUN verdict on real data immediately after the D.1
migration, despite a reconciled cross-tab and a computable strict CLV
sample existing.

The fix is NOT "fall back to all-time forward_clean_n > 0" -- that would
permanently suppress COLLECTION_NOT_RUN forever after the first real
collection day, even on a day where collection genuinely did not run.
Evidence must be TIME-BOUNDED (a rolling lookback window, not a
calendar-day boundary, so a run that crossed midnight is still counted)
and must reflect OPERATIONAL activity (polls, ingests, heartbeats, closes),
not just the cumulative existence of old data.

Report-time inference only -- this module never writes to Settings or any
other table, and never fabricates a run start/end timestamp.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ClosingRecord, MarketAvailabilityRecord, OddsSnapshot, PollCycle, RawProviderResponse

LEGACY_EVIDENCE_LOOKBACK_HOURS = 24.0

ACTIVE_RUN = "ACTIVE_RUN"
COMPLETED_RUN_METADATA = "COMPLETED_RUN_METADATA"
LEGACY_RECENT_ACTIVITY_INFERRED = "LEGACY_RECENT_ACTIVITY_INFERRED"
NO_EVIDENCE = "NO_EVIDENCE"


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def resolve_collection_evidence(db: Session, health: dict, now: datetime,
                                lookback_hours: float = LEGACY_EVIDENCE_LOOKBACK_HOURS) -> dict:
    """`health` is the dict returned by app.routers.ops.health() (or the real
    backend's /api/ops/health JSON). Returns:
        {"collection_has_run": bool, "evidence_source": str, "detail": {...}}
    Evidence priority (first match wins):
      1. ACTIVE_RUN -- health['active_run'] is set (poller currently on).
      2. COMPLETED_RUN_METADATA -- health['last_completed_run'] exists AND
         its run_completed_at falls inside the lookback window.
      3. LEGACY_RECENT_ACTIVITY_INFERRED -- new bookkeeping is absent/stale,
         but real operational activity (poll/ingest/heartbeat/closes/raw
         provider responses) occurred within the same lookback window.
      Otherwise: NO_EVIDENCE -- collection_has_run=False.
    """
    active_run = health.get("active_run")
    if active_run is not None:
        return {"collection_has_run": True, "evidence_source": ACTIVE_RUN,
               "detail": {"active_run": active_run}}

    window_start = now - timedelta(hours=lookback_hours)

    last_completed_run = health.get("last_completed_run")
    if last_completed_run is not None:
        completed_at = _parse_iso(last_completed_run.get("run_completed_at"))
        if completed_at is not None and completed_at >= window_start:
            return {"collection_has_run": True, "evidence_source": COMPLETED_RUN_METADATA,
                   "detail": {"last_completed_run": last_completed_run,
                              "evidence_window_start": window_start.isoformat(),
                              "evidence_window_end": now.isoformat()}}

    # Legacy/migration-boundary fallback: new bookkeeping is absent or stale
    # (e.g. the run completed before D.1's Settings.last_completed_run_*
    # columns existed) -- infer from time-bounded operational evidence
    # instead. Never a calendar-day boundary (a run that crossed midnight
    # must still be detected), never all-time totals.
    last_poll_at = _parse_iso(health.get("last_successful_poll_at"))
    last_ingest_at = _parse_iso(health.get("last_successful_ingest_at"))
    last_heartbeat_at = _parse_iso(health.get("last_availability_heartbeat_at"))

    poll_cycles_recent = db.scalar(select(func.count(PollCycle.id)).where(
        PollCycle.poll_started_at >= window_start)) or 0
    odds_rows_recent = db.scalar(select(func.count(OddsSnapshot.id)).where(
        OddsSnapshot.collected_at >= window_start)) or 0
    heartbeats_recent = db.scalar(select(func.count(MarketAvailabilityRecord.id)).where(
        MarketAvailabilityRecord.observed_at >= window_start)) or 0
    raw_responses_recent = db.scalar(select(func.count(RawProviderResponse.id)).where(
        RawProviderResponse.at >= window_start)) or 0
    forward_clean_rows_recent = db.scalar(select(func.count(ClosingRecord.id)).where(
        ClosingRecord.created_at >= window_start, ClosingRecord.close_polled_at.is_not(None))) or 0

    poll_within_window = last_poll_at is not None and last_poll_at >= window_start
    ingest_within_window = last_ingest_at is not None and last_ingest_at >= window_start
    heartbeat_within_window = last_heartbeat_at is not None and last_heartbeat_at >= window_start

    detail = {
        "evidence_window_start": window_start.isoformat(),
        "evidence_window_end": now.isoformat(),
        "last_successful_poll_at": last_poll_at.isoformat() if last_poll_at else None,
        "last_successful_ingest_at": last_ingest_at.isoformat() if last_ingest_at else None,
        "last_availability_heartbeat_at": last_heartbeat_at.isoformat() if last_heartbeat_at else None,
        "poll_cycles_recent": poll_cycles_recent,
        "odds_rows_recent": odds_rows_recent,
        "heartbeats_recent": heartbeats_recent,
        "raw_provider_responses_recent": raw_responses_recent,
        "forward_clean_rows_recent": forward_clean_rows_recent,
    }

    has_recent_activity = (poll_within_window or ingest_within_window or heartbeat_within_window
                           or poll_cycles_recent > 0 or odds_rows_recent > 0 or heartbeats_recent > 0
                           or raw_responses_recent > 0 or forward_clean_rows_recent > 0)

    if has_recent_activity:
        return {"collection_has_run": True, "evidence_source": LEGACY_RECENT_ACTIVITY_INFERRED, "detail": detail}

    return {"collection_has_run": False, "evidence_source": NO_EVIDENCE, "detail": detail}
