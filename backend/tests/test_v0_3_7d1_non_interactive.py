"""v0.3.7D.1 Task 9/12: safe non-interactive mode on preflight_workday_run.py
-- --yes rejection, --allow-warn gating, accepted-warnings audit trail, and
the discovery-tick retry for zero-upcoming-matches. Never touches the live
active database (imports the script directly and monkeypatches its DB-
touching internals for the parts that would otherwise hit a real DB/API)."""
import importlib.util
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


preflight = _load("ops/preflight_workday_run.py", "v37d1_preflight_test")


def test_yes_flag_is_rejected_with_exact_message(capsys):
    rc = preflight.main(["--yes"])
    assert rc == 1
    captured = capsys.readouterr()
    assert ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
           "FAIL items always stop. Dangerous actions are never auto-confirmed.") in captured.err


def test_warn_without_allow_warn_blocks(monkeypatch):
    monkeypatch.setattr(preflight, "run_checks", lambda: {
        "overall": "WARN", "checks": {"x": {"level": "WARN", "detail": "some warning"}},
        "next_command": "n/a"})
    rc = preflight.main([])
    assert rc == 1


def test_warn_with_allow_warn_continues_and_logs(monkeypatch, tmp_path):
    log_path = tmp_path / "accepted_warnings.jsonl"
    monkeypatch.setattr(preflight, "ACCEPTED_WARNINGS_LOG", log_path)
    monkeypatch.setattr(preflight, "run_checks", lambda: {
        "overall": "WARN", "checks": {"x": {"level": "WARN", "detail": "some warning"}},
        "next_command": "n/a"})
    rc = preflight.main(["--non-interactive", "--allow-warn"])
    assert rc == 0
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["code"] == "x"
    assert entry["text"] == "some warning"
    assert "timestamp" in entry and "run_id" in entry and "command" in entry


def test_fail_always_blocks_even_with_allow_warn(monkeypatch):
    monkeypatch.setattr(preflight, "run_checks", lambda: {
        "overall": "FAIL", "checks": {"x": {"level": "FAIL", "detail": "broken"}},
        "next_command": "n/a"})
    rc = preflight.main(["--allow-warn"])
    assert rc == 1


def test_pass_returns_zero(monkeypatch):
    monkeypatch.setattr(preflight, "run_checks", lambda: {
        "overall": "PASS", "checks": {"x": {"level": "PASS", "detail": "ok"}},
        "next_command": "n/a"})
    rc = preflight.main([])
    assert rc == 0


def test_upcoming_matches_check_retries_discovery_tick_before_warning(monkeypatch):
    calls = {"count": 0}

    def fake_count():
        calls["count"] += 1
        return 0 if calls["count"] == 1 else 3  # empty first, discovery "found" some on retry

    def fake_tick():
        return {"attempted": True, "found": 3, "new": 3}

    monkeypatch.setattr(preflight, "_count_upcoming_matches", fake_count)
    monkeypatch.setattr(preflight, "attempt_discovery_tick", fake_tick)
    level, detail = preflight.check_upcoming_matches()
    assert level == "PASS"
    assert "discovery-tick retry" in detail
    assert calls["count"] == 2


def test_upcoming_matches_check_warns_only_after_retry_still_empty(monkeypatch):
    monkeypatch.setattr(preflight, "_count_upcoming_matches", lambda: 0)
    monkeypatch.setattr(preflight, "attempt_discovery_tick",
                        lambda: {"attempted": True, "found": 0, "new": 0})
    level, detail = preflight.check_upcoming_matches()
    assert level == "WARN"
    assert "discovery-tick retry" in detail
