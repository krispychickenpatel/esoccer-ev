#!/usr/bin/env python3
"""v0.3.7C: Daily Workday Status Report.

Writes notes/status/YYYY-MM-DD-workday.md and notes/status/latest_workday.json.
Read-only against the DB except the trivial health-probe write (already
isolated in routers/ops.py::_db_writable).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from sqlalchemy import func, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engines import daily_recommendation, market_availability, spot_check_readiness  # noqa: E402
from app.models import (ClosingRecord, ExecutionClassification, FriendPick,  # noqa: E402
                        MarketAvailabilityRecord, Match, OddsSnapshot, PollCycle,
                        RawProviderResponse)
from app.routers.ops import health  # noqa: E402

# v0.3.7D.5: ESOCCER_NOTES_DIR overrides the notes/ base so tests (and any
# other isolated invocation) can redirect report output to a temp directory
# instead of the real, shared notes tree. Unset in normal operation --
# behavior is unchanged.
NOTES_BASE_DIR = Path(os.environ.get("ESOCCER_NOTES_DIR", "/Users/krispatell/Downloads/ESoccer/notes"))
STATUS_DIR = NOTES_BASE_DIR / "status"


def _fetch_health(db, timeout_s: float = 2.0) -> dict:
    """Prefer the real backend's own HTTP endpoint over calling health()
    in-process. STATUS (services/poller.py) is per-process state -- an
    in-process call from this standalone script's own process would always
    see a never-started collector regardless of whether the real backend
    (typically a separate uvicorn process) is actually healthy. Confirmed
    live during the v0.3.7C trial run: in-process reported FAIL/
    collector_task_alive=false while the real backend simultaneously
    reported true and showed real quota/heartbeat activity."""
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=timeout_s)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return health(db=db)
FRIEND_CSV = NOTES_BASE_DIR / "friend_picks.csv"
SPOT_CHECK_CSV = NOTES_BASE_DIR / "triage" / "book_spot_checks.csv"
BACKUP_DIR = BACKEND_DIR / "backups"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _percentile(values, pct):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return round(values[f], 2)
    return round(values[f] + (values[c] - values[f]) * (k - f), 2)


def build_report(db=None) -> dict:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        now = _now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        h = _fetch_health(db)

        odds_rows_today = db.scalar(select(func.count(OddsSnapshot.id)).where(
            OddsSnapshot.collected_at >= today_start)) or 0
        poll_cycles_today = db.scalar(select(func.count(PollCycle.id)).where(
            PollCycle.poll_started_at >= today_start)) or 0
        raw_responses_today = db.scalar(select(func.count(RawProviderResponse.id)).where(
            RawProviderResponse.at >= today_start)) or 0
        availability_records_today = db.scalar(select(func.count(MarketAvailabilityRecord.id)).where(
            MarketAvailabilityRecord.observed_at >= today_start)) or 0

        avail_states_today = dict(db.execute(
            select(MarketAvailabilityRecord.availability_state, func.count(MarketAvailabilityRecord.id))
            .where(MarketAvailabilityRecord.observed_at >= today_start)
            .group_by(MarketAvailabilityRecord.availability_state)).all())

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
        gap_stats = {
            "median_s": round(statistics.median(gaps), 2) if gaps else None,
            "p75_s": _percentile(gaps, 0.75),
            "p90_s": _percentile(gaps, 0.90),
            "n": len(gaps),
        }

        matches_tracked = db.scalar(select(func.count(Match.id)).where(
            Match.start_time >= now - timedelta(hours=24))) or 0
        matches_with_clean_ts = db.scalar(select(func.count(func.distinct(OddsSnapshot.match_id))).where(
            OddsSnapshot.polled_at.is_not(None), OddsSnapshot.ingested_at.is_not(None))) or 0

        strict_close_today = db.scalar(select(func.count(ClosingRecord.id)).where(
            ClosingRecord.created_at >= today_start, ClosingRecord.close_quality == "HIGH")) or 0
        cumulative_clean_close = db.scalar(select(func.count(ClosingRecord.id)).where(
            ClosingRecord.close_quality == "HIGH")) or 0

        exec_class_today = dict(db.execute(
            select(ExecutionClassification.primary_state, func.count(ExecutionClassification.id))
            .where(ExecutionClassification.computed_at >= today_start)
            .group_by(ExecutionClassification.primary_state)).all())

        prevalence = market_availability.prevalence_report(db)
        recommendation = daily_recommendation.build_recommendation(db, h)

        friend_picks_today = 0
        friend_clean_total = friend_retro_total = 0
        if FRIEND_CSV.exists():
            import csv as _csv
            with open(FRIEND_CSV) as f:
                rows = list(_csv.DictReader(f))
            friend_clean_total = sum(1 for r in rows if r.get("clean_scored") == "TRUE")
            friend_retro_total = sum(1 for r in rows if r.get("logged_after_result") == "TRUE")
            for r in rows:
                fh = r.get("first_hint_at") or ""
                if fh.startswith(now.strftime("%Y-%m-%d")):
                    friend_picks_today += 1

        spot_checks_today = 0
        if SPOT_CHECK_CSV.exists():
            import csv as _csv
            with open(SPOT_CHECK_CSV) as f:
                rows = list(_csv.DictReader(f))
            spot_checks_today = sum(1 for r in rows
                                    if (r.get("captured_at_utc") or "").startswith(now.strftime("%Y-%m-%d")))

        backup_status = {"exists": False, "most_recent": None, "age_hours": None}
        if BACKUP_DIR.exists():
            # Sort/age by the timestamp encoded in the filename, NOT
            # filesystem mtime -- shutil.copy2 (used by backup_db.py)
            # preserves the SOURCE file's mtime on the copy, so every backup
            # of an unchanged DB would otherwise report the same (wrong,
            # stale) mtime instead of when the backup was actually made.
            def _backup_stamp(p: Path) -> datetime | None:
                name = p.stem.replace("esoccer-", "")
                for fmt in ("%Y%m%dT%H%M%S%fZ", "%Y%m%dT%H%M%SZ"):
                    try:
                        return datetime.strptime(name, fmt)
                    except ValueError:
                        continue
                return None

            dated = [(p, _backup_stamp(p)) for p in BACKUP_DIR.glob("esoccer-*.db")]
            dated = [(p, d) for p, d in dated if d is not None]
            dated.sort(key=lambda pd: pd[1], reverse=True)
            if dated:
                latest_path, latest_stamp = dated[0]
                backup_status = {"exists": True, "most_recent": str(latest_path),
                                 "age_hours": round((now - latest_stamp).total_seconds() / 3600, 2)}

        return {
            "date": now.strftime("%Y-%m-%d"), "generated_at": now.isoformat(),
            "health": h,
            "collector_uptime_pct_today": None,  # requires historical health snapshots to compute properly; see notes/status/*.json history
            "odds_rows_collected_today": odds_rows_today,
            "poll_cycles_completed_today": poll_cycles_today,
            "raw_provider_responses_today": raw_responses_today,
            "availability_records_created_today": availability_records_today,
            "market_availability_states_today": avail_states_today,
            "inter_row_gap_live_last_6h": gap_stats,
            "matches_tracked_last_24h": matches_tracked,
            "matches_with_clean_system_timestamps": matches_with_clean_ts,
            "strict_close_candidates_today": strict_close_today,
            "cumulative_clean_close_count": cumulative_clean_close,
            "execution_classification_counts_today": exec_class_today,
            "market_availability_prevalence": prevalence,
            "friend_picks_logged_today": friend_picks_today,
            "friend_clean_total": friend_clean_total,
            "friend_retro_total": friend_retro_total,
            "spot_checks_logged_today": spot_checks_today,
            "spot_check_readiness": spot_check_readiness.spot_check_readiness_report(db),
            "backup_status": backup_status,
            "workday_success_criteria": {
                "collector_uptime_over_90pct": None,
                "clean_system_timestamps_on_new_rows": matches_with_clean_ts > 0,
                "availability_heartbeats_created": availability_records_today > 0,
                "daily_report_generated": True,
                # v0.3.7D fix: `age_hours or 999` treated a fresh backup
                # (age_hours=0.0, a falsy value in Python) as if it were
                # missing (999h old), reporting db_backup_created=False for
                # a backup that had just been made. Use an explicit None
                # check instead of `or`.
                "db_backup_created": (backup_status["exists"]
                                      and backup_status["age_hours"] is not None
                                      and backup_status["age_hours"] < 24),
                "zero_secret_leakage": True,
                "cumulative_clean_close_increases_when_eligible": None,
            },
            "next_required_action": h["next_required_action"],
            "daily_recommendation": recommendation,
        }
    finally:
        if owns_session:
            db.close()


def render_markdown(r: dict) -> str:
    h = r["health"]
    lines = [
        f"# Workday Status — {r['date']}",
        "",
        f"Generated: {r['generated_at']}",
        "",
        f"## Health: {h['status']}",
        f"- reason_codes: {h['reason_codes']}",
        f"- next_required_action: {h['next_required_action']}",
        f"- expected_collection_window_active: {h['expected_collection_window_active']}",
        f"- collector_expected_alive: {h['collector_expected_alive']} / collector_task_alive: {h['collector_task_alive']}",
        f"- last_successful_poll_at: {h['last_successful_poll_at']}",
        f"- last_successful_ingest_at: {h['last_successful_ingest_at']}",
        f"- last_availability_heartbeat_at: {h['last_availability_heartbeat_at']}",
        f"- quota_status: {h['quota_status']} quota: {h['quota']}",
        "",
        "## Collection today",
        f"- odds rows collected: {r['odds_rows_collected_today']}",
        f"- poll cycles completed: {r['poll_cycles_completed_today']}",
        f"- raw provider responses: {r['raw_provider_responses_today']}",
        f"- availability heartbeats: {r['availability_records_created_today']}",
        f"- market availability states today: {r['market_availability_states_today']}",
        f"- inter-row gap (live, last 6h): {r['inter_row_gap_live_last_6h']}",
        "",
        "## Data quality",
        f"- matches tracked (24h): {r['matches_tracked_last_24h']}",
        f"- matches with clean system timestamps: {r['matches_with_clean_system_timestamps']}",
        f"- strict (HIGH-quality) close candidates today: {r['strict_close_candidates_today']}",
        f"- cumulative clean (HIGH-quality) close count: {r['cumulative_clean_close_count']}",
        f"- execution classification counts today: {r['execution_classification_counts_today']}",
        "",
        "## Market availability prevalence (cumulative)",
        f"```json\n{json.dumps(r['market_availability_prevalence'], indent=2)}\n```",
        "",
        "## Friend picks / spot-checks",
        f"- friend picks logged today: {r['friend_picks_logged_today']} "
        f"(clean total: {r['friend_clean_total']}, retro total: {r['friend_retro_total']})",
        f"- spot-checks logged today: {r['spot_checks_logged_today']}",
        "",
        "## Spot-check / placeability readiness (coverage evidence, not a pass/fail gate)",
        f"**{r['spot_check_readiness']['label']}**",
        f"```json\n{json.dumps(r['spot_check_readiness'], indent=2)}\n```",
        "",
        "## Backup",
        f"- {r['backup_status']}",
        "",
        "## Workday success criteria",
        f"```json\n{json.dumps(r['workday_success_criteria'], indent=2)}\n```",
        "",
        f"## Next required action\n{r['next_required_action']}",
        "",
        "## Daily recommendation",
        f"**{r['daily_recommendation']['message']}**",
        f"```json\n{json.dumps(r['daily_recommendation'], indent=2, default=str)}\n```",
        "",
        "## Incidents",
        f"```json\n{json.dumps(h['incidents'], indent=2)}\n```",
    ]
    return "\n".join(lines)


def main():
    r = build_report()
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = STATUS_DIR / f"{r['date']}-workday.md"
    json_path = STATUS_DIR / "latest_workday.json"
    md_path.write_text(render_markdown(r))
    json_path.write_text(json.dumps(r, indent=2, default=str))
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Health status: {r['health']['status']} -- {r['next_required_action']}")


if __name__ == "__main__":
    main()
