#!/usr/bin/env python3
"""v0.3.7C: Workday Autopilot runner.

Starts (or attaches to an already-running) backend, validates the
environment, turns real odds collection on with a bounded, self-enforcing
runtime cap, monitors health, and writes heartbeat/incident logs.

Never prints secrets. BETSAPI_KEY is only ever checked for presence.

No live betting, no bet placement, no bankroll automation, no model
promotion happens here or anywhere this script touches.

Usage:
    python3 scripts/ops/run_workday_autopilot.py --max-minutes 45
    python3 scripts/ops/run_workday_autopilot.py --max-minutes 480 --caffeinate
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

LOGS_DIR = REPO_DIR / "logs" / "workday"
INCIDENTS_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/status/incidents")

REQUIRED_ENV_VARS = ("BETSAPI_KEY", "BETSAPI_TOKEN")  # either satisfies (see betsapi_provider.py)

YES_FLAG_REJECTION = ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
                     "FAIL items always stop. Dangerous actions are never auto-confirmed.")
REQUIRED_V037B_COLUMNS = {
    "odds_snapshots": ("source_ts", "polled_at", "response_received_at", "ingested_at",
                       "poll_cycle_id", "provider_event_id"),
    "settings": ("autopilot_max_runtime_minutes", "autopilot_started_at",
                "densified_polling_enabled"),
}

_shutdown_requested = False


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_env() -> list[str]:
    """Returns a list of problems (empty = OK). Never prints the key value."""
    problems = []
    if not any(os.environ.get(v) for v in REQUIRED_ENV_VARS):
        problems.append(f"Neither {REQUIRED_ENV_VARS[0]} nor {REQUIRED_ENV_VARS[1]} is set in the environment.")
    return problems


def validate_schema() -> list[str]:
    from sqlalchemy import inspect
    from app.database import engine
    problems = []
    insp = inspect(engine)
    for table, cols in REQUIRED_V037B_COLUMNS.items():
        if not insp.has_table(table):
            problems.append(f"Table {table} does not exist -- start the backend once to run migrations.")
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        missing = [c for c in cols if c not in existing]
        if missing:
            problems.append(f"{table} missing columns {missing} -- restart the backend to apply migrations.")
    return problems


def db_writable() -> bool:
    from app.routers.ops import _db_writable
    return _db_writable()


def backend_already_running(port: int = 8000) -> bool:
    import httpx
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def start_backend(caffeinate: bool) -> subprocess.Popen | None:
    cmd = []
    if caffeinate:
        cmd = ["caffeinate", "-i"]
    cmd += [str(BACKEND_DIR / ".venv" / "bin" / "uvicorn"), "app.main:app",
           "--host", "127.0.0.1", "--port", "8000"]
    log_path = BACKEND_DIR / "uvicorn.workday.log"
    log_f = open(log_path, "a")
    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR), stdout=log_f, stderr=log_f)
    print(f"Started backend pid={proc.pid} (log: {log_path})")
    return proc


def set_autopilot(max_minutes: int, densified: bool):
    from app.database import SessionLocal
    from app.models import Settings
    db = SessionLocal()
    try:
        s = db.get(Settings, 1)
        s.poller_enabled = True
        s.autopilot_started_at = _now()
        s.autopilot_max_runtime_minutes = max_minutes
        if densified:
            s.densified_polling_enabled = True
        db.commit()
    finally:
        db.close()


def stop_autopilot():
    from app.database import SessionLocal
    from app.models import Settings
    db = SessionLocal()
    try:
        s = db.get(Settings, 1)
        if s:
            # v0.3.7D.1: persist the completed-run window BEFORE clearing the
            # live autopilot_started_at/autopilot_max_runtime_minutes fields --
            # see the model comment on last_completed_run_* -- otherwise
            # reporting has no way to know a run just finished vs. never
            # started, and reports the wrong (generic) status.
            if s.autopilot_started_at is not None:
                s.last_completed_run_started_at = s.autopilot_started_at
                s.last_completed_run_completed_at = _now()
                s.last_completed_run_max_minutes = s.autopilot_max_runtime_minutes
            s.poller_enabled = False
            s.autopilot_started_at = None
            s.autopilot_max_runtime_minutes = None
            db.commit()
    finally:
        db.close()


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def write_heartbeat_log(entry: dict):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def write_incident(entry: dict):
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = INCIDENTS_DIR / f"incident-{stamp}.json"
    path.write_text(json.dumps(entry, indent=2, default=str))


def _db_snapshot_count() -> int | None:
    """v0.3.7D.1 Task 10: DB-progress fallback. Read directly (never write)
    to check whether the collector is still making progress even when this
    monitor's OWN http client can't reach the health endpoint -- an HTTP
    problem (this process, network, port) is not proof the collector died."""
    try:
        from app.database import SessionLocal
        from app.models import OddsSnapshot
        from sqlalchemy import func, select
        db = SessionLocal()
        try:
            return db.scalar(select(func.count(OddsSnapshot.id)))
        finally:
            db.close()
    except Exception:
        return None


def _db_poller_enabled() -> bool:
    """Read-only fallback for whether the poller is enabled, used only when
    the HTTP health endpoint itself is unreachable."""
    try:
        from app.database import SessionLocal
        from app.models import Settings
        db = SessionLocal()
        try:
            s = db.get(Settings, 1)
            return bool(s and s.poller_enabled)
        finally:
            db.close()
    except Exception:
        return True  # fail open -- don't stop the monitor loop on a read error


def classify_http_failure(consecutive_failures: int) -> tuple[str, str]:
    """v0.3.7D.1 Task 10: escalation, not an instant FAIL, on the FIRST http
    timeout -- a single slow response is common and not evidence of a dead
    collector. Resets to 0 by the caller after any successful call."""
    if consecutive_failures <= 1:
        return "WARN", "MONITOR_HTTP_TIMEOUT_TRANSIENT"
    if consecutive_failures == 2:
        return "DEGRADED", "MONITOR_HTTP_TIMEOUT_REPEATED"
    return "FAIL", "MONITOR_HTTP_ERROR"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-minutes", type=int, required=True,
                    help="hard auto-shutoff after this many minutes")
    ap.add_argument("--caffeinate", action="store_true",
                    help="wrap a freshly-started backend in caffeinate -i (prevents Mac sleep). "
                        "No effect if a backend is already running (see run_autopilot for full-day use).")
    ap.add_argument("--densified", action="store_true",
                    help="also enable densified near-kickoff polling (requires WORKDAY_ENABLE_DENSIFIED_POLLING=true too)")
    ap.add_argument("--check-interval-s", type=int, default=60)
    ap.add_argument("--non-interactive", action="store_true",
                    help="assert non-interactive operation (this script never prompts anyway)")
    ap.add_argument("--allow-warn", action="store_true",
                    help="accepted for contract consistency with the other ops scripts; every FAIL-level "
                         "check in this script already stops unconditionally, so this does not relax anything")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.yes:
        print(f"FAIL: {YES_FLAG_REJECTION}", file=sys.stderr)
        sys.exit(1)

    print("=== v0.3.7C Workday Autopilot ===")
    print("No live betting. No bet placement. No bankroll automation. No model promotion.")
    print("")

    # app.database loads backend/.env via python-dotenv at import time (see
    # database.py) -- must happen before validate_env() checks os.environ,
    # otherwise a real, correctly-configured .env key looks "missing" simply
    # because nothing has imported the app package yet.
    import app.database  # noqa: F401

    env_problems = validate_env()
    if env_problems:
        for p in env_problems:
            print(f"FAIL: {p}", file=sys.stderr)
        sys.exit(1)
    print("OK: required environment variable is set (value never printed).")

    if not db_writable():
        print("FAIL: database is not writable.", file=sys.stderr)
        sys.exit(1)
    print("OK: database is writable.")

    schema_problems = validate_schema()
    if schema_problems:
        for p in schema_problems:
            print(f"FAIL: {p}", file=sys.stderr)
        sys.exit(1)
    print("OK: v0.3.7B schema fields present.")

    proc = None
    if backend_already_running():
        print("Backend already running on port 8000 -- attaching to it instead of starting a new one.")
    else:
        proc = start_backend(caffeinate=args.caffeinate)
        for _ in range(20):
            if backend_already_running():
                break
            time.sleep(0.5)
        else:
            print("FAIL: backend did not come up in time.", file=sys.stderr)
            sys.exit(1)

    if args.densified and not os.environ.get("WORKDAY_ENABLE_DENSIFIED_POLLING", "").lower() in ("1", "true", "yes"):
        print("NOTE: --densified requested but WORKDAY_ENABLE_DENSIFIED_POLLING is not set -- "
              "densified polling will stay OFF (both gates are required).")

    set_autopilot(args.max_minutes, args.densified)
    print(f"Autopilot STARTED: cap={args.max_minutes} minutes.")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # v0.3.7C bug fix: health must be checked via the ACTUAL backend's HTTP
    # endpoint, not by importing routers.ops.health() and calling it
    # in-process. STATUS (services/poller.py) is a plain module-level dict --
    # it only reflects reality inside the SAME process that is running
    # poll_loop. When this monitor attaches to an already-running backend
    # (the common case), calling health() in-process reads THIS process's
    # own (never-started) STATUS, which always looks like the collector is
    # dead even though the real backend is fine. Confirmed live: in-process
    # call reported collector_task_alive=false/FAIL while the real backend's
    # own /api/ops/health simultaneously reported collector_task_alive=true
    # and real quota/heartbeat activity.
    import httpx

    start = time.monotonic()
    max_seconds = args.max_minutes * 60
    consecutive_http_failures = 0
    last_db_count = _db_snapshot_count()
    try:
        while not _shutdown_requested:
            try:
                h = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=10).json()
                consecutive_http_failures = 0  # v0.3.7D.1 Task 10: reset after any success
            except Exception as e:
                consecutive_http_failures += 1
                status, reason = classify_http_failure(consecutive_http_failures)
                # v0.3.7D.1 Task 10: DB-progress fallback. The collector may
                # be perfectly healthy even though THIS monitor process can't
                # reach the HTTP endpoint (network blip, port contention,
                # etc.) -- never claim collector failure while the DB itself
                # is still visibly making progress.
                db_count = _db_snapshot_count()
                if db_count is not None and last_db_count is not None and db_count > last_db_count:
                    status, reason = "DEGRADED_MONITOR_ONLY", "MONITOR_HTTP_UNREACHABLE_BUT_DB_PROGRESSING"
                if db_count is not None:
                    last_db_count = db_count
                h = {"status": status, "reason_codes": [reason],
                    "poller_enabled_in_settings": _db_poller_enabled()}
                print(f"{status}: could not reach backend health endpoint "
                     f"(consecutive_failures={consecutive_http_failures}): {e}")
            entry = {"at": _now().isoformat(), "status": h["status"], "reason_codes": h["reason_codes"]}
            write_heartbeat_log(entry)
            print(f"[{entry['at']}] health={h['status']} reasons={h['reason_codes']}")
            if h["status"] == "FAIL":
                write_incident(h)
            if not h["poller_enabled_in_settings"] and time.monotonic() - start > 5:
                print("Autopilot cap reached (poller_enabled auto-disabled by poll_loop). Exiting monitor.")
                break
            if time.monotonic() - start > max_seconds + 30:
                print("Safety exit: past the configured cap and poller still enabled -- check poll_loop.")
                break
            time.sleep(args.check_interval_s)
    finally:
        if _shutdown_requested:
            print("Shutdown requested -- disabling poller_enabled safely.")
            stop_autopilot()
        print("Workday Autopilot monitor exiting. Backend process left running for continued use.")
        if proc is not None:
            print(f"(Backend was started by this script, pid={proc.pid} -- it is NOT being killed; "
                  f"stop it yourself with `kill {proc.pid}` if you want it down too.)")


if __name__ == "__main__":
    main()
