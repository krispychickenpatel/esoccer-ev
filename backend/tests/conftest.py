"""v0.3.7D.5 reliability hotfix: global fail-fast guard against running the
test suite while a live unattended run is in progress.

This exists because of a real incident (notes/triage/v0_3_7D4-sleep-hang-incident.md
and notes/triage/v0_3_7D5-reliability-hotfix.md): the full backend test
suite was run while a real 480-minute collection was active, and one
existing smoke test (test_v0_3_7d2_daily_cycle_hotfix.py) copied a backup
directly on top of the live `backend/esoccer.db`, then never restored it
because the test process was killed before its own `finally` block ran.
Net result: ~3 minutes of real collection data lost and a false SUCCESS
status nearly got written for a run that never actually completed.

This check is intentionally dependency-free (no app/SQLAlchemy imports, no
engine creation) so it can never itself trigger a migration or write, and
it always inspects the REAL default production paths (backend/esoccer.db,
logs/unattended/run.lock) regardless of any DATABASE_URL or
ESOCCER_NOTES_DIR override an individual test might set later in the
session -- those overrides exist so tests can point AWAY from production,
and this guard's whole job is to notice if the actual, real production
system looks busy before any test gets a chance to run at all.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
LIVE_DB_PATH = BACKEND_DIR / "esoccer.db"
LOCK_PATH = REPO_DIR / "logs" / "unattended" / "run.lock"

ALLOW_ENV_VAR = "ESOCCER_ALLOW_TESTS_DURING_LIVE_RUN"


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just owned by someone else -- still alive
    return True


def detect_active_live_run(db_path: Path = LIVE_DB_PATH, lock_path: Path = LOCK_PATH) -> str | None:
    """Returns a human-readable reason string if a live unattended run
    appears active, else None. Two independent, read-only signals:
    1. the orchestrator's own single-instance lock, with a live PID;
    2. Settings.poller_enabled=True in the real production database, read
       via a raw read-only sqlite3 connection -- never through the app
       package or SQLAlchemy, so this can't itself create/migrate a DB."""
    if lock_path.exists():
        try:
            holder = json.loads(lock_path.read_text())
        except (OSError, json.JSONDecodeError):
            holder = None
        if holder is not None:
            pid = holder.get("pid")
            if _pid_alive(pid):
                return (f"live unattended-run lock held by pid={pid} "
                       f"(run_id={holder.get('run_id')!r}) at {lock_path}")

    if db_path.exists():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute("SELECT poller_enabled FROM settings WHERE id = 1").fetchone()
            finally:
                con.close()
            if row and row[0]:
                return f"Settings.poller_enabled=True in {db_path} -- a collection run appears active"
        except sqlite3.Error:
            pass  # can't read it read-only -- don't block solely on that

    return None


def pytest_sessionstart(session):
    if os.environ.get(ALLOW_ENV_VAR):
        return
    reason = detect_active_live_run()
    if reason:
        import pytest
        pytest.exit(
            "\n\nFAIL-FAST: refusing to run the test suite -- " + reason + ".\n"
            "Running tests (especially subprocess-based smoke tests) against a live, "
            "in-progress unattended run risks touching production state -- see "
            "notes/triage/v0_3_7D4-sleep-hang-incident.md for exactly what that cost.\n"
            f"Wait for the run to finish, or set {ALLOW_ENV_VAR}=1 if you have "
            "independently verified it is safe to proceed.\n",
            returncode=2,
        )
