"""Watchdog / operational health (v0.3.7B Section 10). Read-mostly; the only
write is the trivial DB-writable probe (a no-op transaction, rolled back)."""
from __future__ import annotations

import os
import shutil
import statistics
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..database import DATABASE_URL, engine, get_db
from ..models import MarketAvailabilityRecord, OddsSnapshot, PollCycle

router = APIRouter(prefix="/api/ops", tags=["ops"])


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

    last_tick_age_s = None
    if STATUS.get("last_tick"):
        try:
            last_tick_age_s = round((now - datetime.fromisoformat(STATUS["last_tick"])).total_seconds(), 1)
        except ValueError:
            last_tick_age_s = None

    last_snap = db.scalar(select(OddsSnapshot).order_by(OddsSnapshot.id.desc()))
    last_odds_row_age_s = None
    if last_snap is not None and last_snap.ingested_at is not None:
        last_odds_row_age_s = round((now - last_snap.ingested_at).total_seconds(), 1)
    elif last_snap is not None:
        last_odds_row_age_s = round((now - last_snap.collected_at).total_seconds(), 1)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    snapshots_today = db.scalar(select(func.count(OddsSnapshot.id)).where(
        OddsSnapshot.collected_at >= today_start)) or 0
    availability_records_today = db.scalar(select(func.count(MarketAvailabilityRecord.id)).where(
        MarketAvailabilityRecord.observed_at >= today_start)) or 0

    # median inter-row gap near kickoff (last 10 min before kickoff, live rows only)
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
    quota = None
    if last_poll_cycle is not None:
        quota = {"quota_limit": last_poll_cycle.quota_limit,
                 "quota_remaining": last_poll_cycle.quota_remaining,
                 "quota_reset_at": last_poll_cycle.quota_reset_at,
                 "as_of": last_poll_cycle.poll_started_at.isoformat()}

    incidents = []
    if STATUS.get("prediction_lab_error"):
        incidents.append({"type": "prediction_lab_error", "detail": STATUS["prediction_lab_error"]})
    if STATUS.get("result_ingestion_error"):
        incidents.append({"type": "result_ingestion_error", "detail": STATUS["result_ingestion_error"]})
    if STATUS.get("friend_pick_resolution_error"):
        incidents.append({"type": "friend_pick_resolution_error", "detail": STATUS["friend_pick_resolution_error"]})
    if last_tick_age_s is not None and last_tick_age_s > 300:
        incidents.append({"type": "stale_poller", "detail": f"last tick {last_tick_age_s}s ago"})

    db_writable = _db_writable()
    if not db_writable:
        incidents.append({"type": "db_not_writable", "detail": "health probe write failed"})

    return {
        "checked_at": now.isoformat(),
        "collector_alive": bool(STATUS.get("running")),
        "collector_note": STATUS.get("note"),
        "last_poll_tick_age_s": last_tick_age_s,
        "last_odds_row_ingested_age_s": last_odds_row_age_s,
        "snapshots_created_today": snapshots_today,
        "availability_records_created_today": availability_records_today,
        "median_inter_row_gap_s_live_last_6h": median_inter_row_gap_s,
        "quota": quota,
        "densified_polling": STATUS.get("densified_polling"),
        "db_writable": db_writable,
        "disk_headroom_mb": _disk_headroom_mb(),
        "incidents": incidents,
        "status": "OK" if (db_writable and not incidents) else "DEGRADED" if db_writable else "CRITICAL",
    }
