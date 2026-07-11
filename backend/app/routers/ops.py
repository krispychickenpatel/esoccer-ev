"""Watchdog / operational health (v0.3.7B Section 10, v0.3.7C 4-level fix).
Read-mostly; the only write is the trivial DB-writable probe (a no-op
transaction, rolled back).

v0.3.7C fix: the v0.3.7B version computed status from db_writable +
an incidents list that never checked "is the collector actually expected
to be running right now, and is it." That let a daily status file say
status=OK while collector_alive=false, snapshots_created_today=0, and the
last odds row was hours stale -- tolerable for a dashboard a human is
actively reading, actively misleading for unattended workday collection.

Four levels, in priority order:
  FAIL     - DB not writable, disk critically low, OR collection is expected
             right now (poller enabled + inside the collection window) but
             the collector task isn't alive / never ticked.
  IDLE     - collection is NOT expected right now (poller disabled, or
             outside the configured WORKDAY_COLLECTION_START/END window).
             Not a failure -- turning the poller off on purpose, or it
             simply being off-hours, is normal.
  DEGRADED - collection is expected and the task is alive, but something is
             stale/missing/unknown (poll tick stale, odds row stale, quota
             status unknown, zero availability heartbeats, zero snapshots
             today).
  OK       - collection is expected, alive, fresh, and nothing above fired.

v0.3.7D: added `state_detail`, a more specific label alongside `status`
(which keeps its 4-level severity meaning unchanged -- this does not
soften FAIL to a lesser severity for the collector-expected-but-dead case,
it only adds a more descriptive name):
  IDLE_POLLER_DISABLED       - status=IDLE, poller_enabled=False, and not
                               due to a just-completed autopilot run.
  IDLE_AFTER_COMPLETED_RUN   - status=IDLE, poller_enabled=False, and
                               STATUS shows a recent autopilot_auto_disabled_at
                               (poll_loop's own bounded-runtime cap fired).
  IDLE_OUTSIDE_COLLECTION_WINDOW - status=IDLE because "now" falls outside
                               the configured WORKDAY_COLLECTION_START/END
                               window (independent of poller_enabled).
  DEGRADED_EXPECTED_BUT_NOT_RUNNING - descriptive alias for the
                               status=FAIL, COLLECTOR_NOT_ALIVE /
                               COLLECTOR_NEVER_TICKED case: collection was
                               expected right now but the task isn't alive.
"""
from __future__ import annotations

import os
import shutil
import statistics
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..database import DATABASE_URL, engine, get_db
from ..models import MarketAvailabilityRecord, OddsSnapshot, PollCycle, Settings
from ..workday_config import load_workday_config

router = APIRouter(prefix="/api/ops", tags=["ops"])

NEXT_ACTION_BY_REASON = {
    "DB_NOT_WRITABLE": "Investigate DB file permissions/disk space immediately -- writes are failing.",
    "DISK_LOW": "Free up disk space; below the configured WORKDAY_MIN_DISK_HEADROOM_MB floor.",
    "COLLECTOR_NOT_ALIVE": "Restart the backend process -- poller_enabled=True but the collector task is not running.",
    "COLLECTOR_NEVER_TICKED": "Restart the backend process -- poller_enabled=True but no tick has ever been recorded.",
    "POLLER_DISABLED": "Run scripts/ops/run_workday_autopilot.py (or set Settings.poller_enabled=True) to start collection.",
    "OUTSIDE_COLLECTION_WINDOW": "No action needed -- outside the configured collection window.",
    "STALE_POLL": "Check network/API connectivity -- collector is alive but hasn't ticked recently.",
    "STALE_INGEST": "Check the poller loop -- ticking but not writing fresh odds rows.",
    "QUOTA_UNKNOWN": "No X-RateLimit-* headers observed yet -- treat quota as unknown, not safe, until a real call succeeds.",
    "NO_AVAILABILITY_HEARTBEATS": "Confirm process_snapshots() is being called -- zero heartbeat rows written today.",
    "NO_SNAPSHOTS_TODAY": "Confirm tracked_leagues/sportsbooks_tracked are non-empty and BETSAPI_KEY is set.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db_writable() -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS _ops_health_probe (id INTEGER)"))
            conn.execute(text("DROP TABLE IF EXISTS _ops_health_probe"))
        return True
    except Exception:
        return False


def _disk_headroom_mb() -> float | None:
    try:
        path = DATABASE_URL.replace("sqlite:///", "") if DATABASE_URL.startswith("sqlite:///") else "."
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".")
        return round(usage.free / (1024 * 1024), 1)
    except Exception:
        return None


@router.get("/health")
def health(db: Session = Depends(get_db)):
    from ..services.poller import STATUS
    now = _now()
    cfg = load_workday_config()

    last_tick_age_s = None
    last_successful_poll_at = STATUS.get("last_tick")
    if last_successful_poll_at:
        try:
            last_tick_age_s = round((now - datetime.fromisoformat(last_successful_poll_at)).total_seconds(), 1)
        except ValueError:
            last_tick_age_s = None

    last_snap = db.scalar(select(OddsSnapshot).order_by(OddsSnapshot.id.desc()))
    last_odds_row_age_s = None
    last_successful_ingest_at = None
    if last_snap is not None and last_snap.ingested_at is not None:
        last_odds_row_age_s = round((now - last_snap.ingested_at).total_seconds(), 1)
        last_successful_ingest_at = last_snap.ingested_at.isoformat()
    elif last_snap is not None:
        last_odds_row_age_s = round((now - last_snap.collected_at).total_seconds(), 1)

    last_heartbeat = db.scalar(select(MarketAvailabilityRecord).order_by(MarketAvailabilityRecord.id.desc()))
    last_availability_heartbeat_at = last_heartbeat.observed_at.isoformat() if last_heartbeat else None

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    snapshots_today = db.scalar(select(func.count(OddsSnapshot.id)).where(
        OddsSnapshot.collected_at >= today_start)) or 0
    availability_records_today = db.scalar(select(func.count(MarketAvailabilityRecord.id)).where(
        MarketAvailabilityRecord.observed_at >= today_start)) or 0

    recent_live = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.phase == "live", OddsSnapshot.collected_at >= now - timedelta(hours=6),
    ).order_by(OddsSnapshot.collected_at)).all()
    gaps = []
    by_match: dict[int, list] = {}
    for r in recent_live:
        by_match.setdefault(r.match_id, []).append(r.collected_at)
    for times in by_match.values():
        times.sort()
        for a, b in zip(times, times[1:]):
            gaps.append((b - a).total_seconds())
    median_inter_row_gap_s = round(statistics.median(gaps), 2) if gaps else None

    last_poll_cycle = db.scalar(select(PollCycle).order_by(PollCycle.id.desc()))
    quota_status = "UNKNOWN"
    quota = None
    if last_poll_cycle is not None:
        quota = {"quota_limit": last_poll_cycle.quota_limit,
                 "quota_remaining": last_poll_cycle.quota_remaining,
                 "quota_reset_at": last_poll_cycle.quota_reset_at,
                 "as_of": last_poll_cycle.poll_started_at.isoformat()}
        if last_poll_cycle.quota_limit is not None and last_poll_cycle.quota_remaining is not None:
            quota_status = "KNOWN"

    other_incidents = []
    if STATUS.get("prediction_lab_error"):
        other_incidents.append({"type": "prediction_lab_error", "detail": STATUS["prediction_lab_error"]})
    if STATUS.get("result_ingestion_error"):
        other_incidents.append({"type": "result_ingestion_error", "detail": STATUS["result_ingestion_error"]})
    if STATUS.get("friend_pick_resolution_error"):
        other_incidents.append({"type": "friend_pick_resolution_error", "detail": STATUS["friend_pick_resolution_error"]})
    if STATUS.get("auto_paper_sim_error"):
        other_incidents.append({"type": "auto_paper_sim_error", "detail": STATUS["auto_paper_sim_error"]})

    db_writable = _db_writable()
    disk_headroom_mb = _disk_headroom_mb()
    settings_row = db.get(Settings, 1)
    poller_enabled_in_settings = bool(settings_row and settings_row.poller_enabled)
    collector_task_alive = bool(STATUS.get("running"))
    expected_collection_window_active = cfg.in_collection_window(now)
    collector_expected_alive = poller_enabled_in_settings and expected_collection_window_active

    reason_codes: list[str] = []

    if not db_writable:
        reason_codes.append("DB_NOT_WRITABLE")
    if disk_headroom_mb is not None and disk_headroom_mb < cfg.min_disk_headroom_mb:
        reason_codes.append("DISK_LOW")
    hard_fail = bool(reason_codes)

    state_detail = None
    if hard_fail:
        status = "FAIL"
    elif not collector_expected_alive:
        status = "IDLE"
        if not expected_collection_window_active:
            reason_codes.append("OUTSIDE_COLLECTION_WINDOW")
            state_detail = "IDLE_OUTSIDE_COLLECTION_WINDOW"
        elif not poller_enabled_in_settings:
            reason_codes.append("POLLER_DISABLED")
            # v0.3.7D: distinguish "never started / manually turned off"
            # from "an autopilot run just completed its bounded cap" --
            # poll_loop sets STATUS["autopilot_auto_disabled_at"] itself
            # the moment it auto-disables poller_enabled.
            if STATUS.get("autopilot_auto_disabled_at"):
                state_detail = "IDLE_AFTER_COMPLETED_RUN"
            else:
                state_detail = "IDLE_POLLER_DISABLED"
    elif not collector_task_alive:
        status = "FAIL"
        reason_codes.append("COLLECTOR_NOT_ALIVE")
        state_detail = "DEGRADED_EXPECTED_BUT_NOT_RUNNING"
    elif last_tick_age_s is None:
        status = "FAIL"
        reason_codes.append("COLLECTOR_NEVER_TICKED")
        state_detail = "DEGRADED_EXPECTED_BUT_NOT_RUNNING"
    else:
        degraded_reasons = []
        if last_tick_age_s > cfg.max_last_poll_age_s:
            degraded_reasons.append("STALE_POLL")
        if last_odds_row_age_s is not None and last_odds_row_age_s > cfg.max_last_ingest_age_s:
            degraded_reasons.append("STALE_INGEST")
        if quota_status == "UNKNOWN":
            degraded_reasons.append("QUOTA_UNKNOWN")
        if availability_records_today == 0:
            degraded_reasons.append("NO_AVAILABILITY_HEARTBEATS")
        if snapshots_today == 0:
            degraded_reasons.append("NO_SNAPSHOTS_TODAY")
        reason_codes.extend(degraded_reasons)
        status = "DEGRADED" if (degraded_reasons or other_incidents) else "OK"

    incidents = [{"type": r, "detail": NEXT_ACTION_BY_REASON.get(r, "")} for r in reason_codes] + other_incidents
    next_required_action = (NEXT_ACTION_BY_REASON.get(reason_codes[0])
                            if reason_codes else "No action needed -- collection healthy.")

    return {
        "checked_at": now.isoformat(),
        "status": status,
        "state_detail": state_detail,
        "reason_codes": reason_codes,
        "next_required_action": next_required_action,
        "expected_collection_window_active": expected_collection_window_active,
        "collector_expected_alive": collector_expected_alive,
        "poller_enabled_in_settings": poller_enabled_in_settings,
        "collector_task_alive": collector_task_alive,
        "collector_note": STATUS.get("note"),
        "last_poll_tick_age_s": last_tick_age_s,
        "last_odds_row_ingested_age_s": last_odds_row_age_s,
        "last_successful_poll_at": last_successful_poll_at,
        "last_successful_ingest_at": last_successful_ingest_at,
        "last_availability_heartbeat_at": last_availability_heartbeat_at,
        "snapshots_created_today": snapshots_today,
        "availability_records_created_today": availability_records_today,
        "median_inter_row_gap_s_live_last_6h": median_inter_row_gap_s,
        "quota": quota,
        "quota_status": quota_status,
        "densified_polling": STATUS.get("densified_polling"),
        "db_writable": db_writable,
        "disk_headroom_mb": disk_headroom_mb,
        "incidents": incidents,
    }
