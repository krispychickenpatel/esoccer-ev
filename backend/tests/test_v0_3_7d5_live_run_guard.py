"""v0.3.7D.5 reliability hotfix Task 2: the conftest.py fail-fast guard that
refuses to run the test suite while a live unattended run appears active.
Tests the detection function directly (tmp_path-based, never touching the
real backend/esoccer.db or logs/unattended/run.lock) -- exercising the real
pytest_sessionstart hook would require a nested pytest session, which is
unnecessary to prove the detection logic itself is correct."""
import json
import os
import sqlite3
import time

import pytest

from tests import conftest as guard


def test_no_signals_returns_none(tmp_path):
    db_path = tmp_path / "esoccer.db"
    lock_path = tmp_path / "run.lock"
    assert guard.detect_active_live_run(db_path, lock_path) is None


def test_live_lock_with_alive_pid_is_detected(tmp_path):
    db_path = tmp_path / "esoccer.db"
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "abc123",
                                     "repo_path": "/x", "created_at": time.time()}))
    reason = guard.detect_active_live_run(db_path, lock_path)
    assert reason is not None
    assert "abc123" in reason


def test_stale_lock_with_dead_pid_is_not_detected(tmp_path):
    db_path = tmp_path / "esoccer.db"
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(json.dumps({"pid": 999999, "run_id": "dead-run",
                                     "repo_path": "/x", "created_at": time.time() - 99999}))
    assert guard.detect_active_live_run(db_path, lock_path) is None


def test_unreadable_lock_file_is_not_detected(tmp_path):
    """Fails open for a corrupt lock file -- the guard's job is to detect a
    GENUINELY live run, not to be a second copy of the lock module's own
    (intentionally fail-closed) acquire() logic."""
    db_path = tmp_path / "esoccer.db"
    lock_path = tmp_path / "run.lock"
    lock_path.write_text("not json {{{")
    assert guard.detect_active_live_run(db_path, lock_path) is None


def _make_db(path, poller_enabled: bool):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, poller_enabled INTEGER)")
    con.execute("INSERT INTO settings (id, poller_enabled) VALUES (1, ?)", (int(poller_enabled),))
    con.commit()
    con.close()


def test_poller_enabled_true_in_db_is_detected(tmp_path):
    db_path = tmp_path / "esoccer.db"
    _make_db(db_path, poller_enabled=True)
    lock_path = tmp_path / "run.lock"  # no lock file
    reason = guard.detect_active_live_run(db_path, lock_path)
    assert reason is not None
    assert "poller_enabled" in reason


def test_poller_enabled_false_in_db_is_not_detected(tmp_path):
    db_path = tmp_path / "esoccer.db"
    _make_db(db_path, poller_enabled=False)
    lock_path = tmp_path / "run.lock"
    assert guard.detect_active_live_run(db_path, lock_path) is None


def test_missing_db_and_lock_is_not_detected(tmp_path):
    db_path = tmp_path / "does_not_exist.db"
    lock_path = tmp_path / "does_not_exist.lock"
    assert guard.detect_active_live_run(db_path, lock_path) is None


def test_corrupt_db_file_is_not_detected_alone(tmp_path):
    """A DB that can't even be read read-only must not block the whole
    suite by itself -- that would be a DB_INTEGRITY_FAILURE case for the
    orchestrator to report, not a reason to refuse to test at all."""
    db_path = tmp_path / "esoccer.db"
    db_path.write_bytes(b"not a real sqlite file" * 50)
    lock_path = tmp_path / "run.lock"
    assert guard.detect_active_live_run(db_path, lock_path) is None


def test_guard_checks_real_default_paths_by_default():
    """The module-level defaults must point at the ACTUAL production
    paths (backend/esoccer.db, logs/unattended/run.lock) relative to this
    repo -- not something a test's own DATABASE_URL/ESOCCER_NOTES_DIR
    override could accidentally redirect."""
    assert guard.LIVE_DB_PATH.name == "esoccer.db"
    assert guard.LIVE_DB_PATH.parent.name == "backend"
    assert guard.LOCK_PATH.parts[-2:] == ("unattended", "run.lock")


def test_allow_env_var_name_is_documented_and_stable():
    assert guard.ALLOW_ENV_VAR == "ESOCCER_ALLOW_TESTS_DURING_LIVE_RUN"
