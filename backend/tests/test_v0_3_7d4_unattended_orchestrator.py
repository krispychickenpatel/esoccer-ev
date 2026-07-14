"""v0.3.7D.4 Task 12: orchestrator-level acceptance tests -- schedule/catch-up
policy, DB integrity check, credential check, backend-lifecycle helpers
(mocked, never touching a real port/process), LaunchAgent plist rendering
and installer/uninstaller safety, and the no-input()/no-secret-leak
guarantees. Never runs test collection against the live database and never
actually invokes launchctl for real."""
import importlib.util
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_OPS = REPO_DIR / "scripts" / "ops"


def _load(rel_name: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_OPS / rel_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


orchestrator = _load("run_unattended_workday.py", "v37d4_orchestrator")


# --------------------------------------------------- schedule / catch-up policy

def _cfg(**overrides):
    base = {"scheduled_hour": 2, "scheduled_minute": 0, "catch_up_hours": 6.0,
           "min_hours_between_runs": 18.0}
    base.update(overrides)
    return base


def test_proceed_when_no_prior_run_and_inside_window():
    now = datetime(2026, 7, 14, 3, 0, 0)  # 1h after the 02:00 schedule
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg())
    assert decision == "PROCEED"


def test_skipped_recent_run_inside_min_spacing():
    last_end = datetime(2026, 7, 14, 10, 0, 0)
    now = last_end + timedelta(hours=5)  # well below 18h minimum spacing
    latest = {"actual_end": last_end.isoformat(), "acceptance_test": False}
    decision, reason = orchestrator.evaluate_schedule(now, latest, _cfg())
    assert decision == "SKIPPED_RECENT_RUN"


def test_acceptance_test_run_never_counts_toward_spacing():
    last_end = datetime(2026, 7, 14, 2, 5, 0)
    now = last_end + timedelta(hours=1)
    latest = {"actual_end": last_end.isoformat(), "acceptance_test": True}
    decision, reason = orchestrator.evaluate_schedule(now, latest, _cfg())
    assert decision == "PROCEED"


def test_missed_window_outside_catchup():
    now = datetime(2026, 7, 14, 10, 0, 0)  # 8h after 02:00, catch-up is only 6h
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg())
    assert decision == "MISSED_WINDOW"


def test_catchup_inside_window():
    now = datetime(2026, 7, 14, 6, 0, 0)  # 4h after 02:00, inside the 6h catch-up
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg())
    assert decision == "PROCEED"


def test_midnight_crossing_schedule_check():
    """now is just after local midnight, before today's 02:00 schedule --
    must evaluate against YESTERDAY's scheduled time, not treat midnight as
    a hard boundary that resets everything to 'no schedule yet'."""
    now = datetime(2026, 7, 14, 0, 30, 0)
    decision, reason = orchestrator.evaluate_schedule(now, None, _cfg())
    # yesterday's schedule (07-13 02:00) + 6h catch-up = 07-13 08:00, long past
    assert decision == "MISSED_WINDOW"

    now2 = datetime(2026, 7, 14, 1, 59, 0)
    decision2, _ = orchestrator.evaluate_schedule(now2, None, _cfg(catch_up_hours=30.0))
    assert decision2 == "PROCEED"


def test_never_starts_multiple_catchup_runs_same_window():
    """A completed catch-up run recorded as actual_end inside today's window
    must trigger SKIPPED_RECENT_RUN (via the min-spacing check) for a second
    invocation shortly after, not another catch-up."""
    now = datetime(2026, 7, 14, 5, 0, 0)
    latest = {"actual_end": datetime(2026, 7, 14, 3, 0, 0).isoformat(), "acceptance_test": False}
    decision, _ = orchestrator.evaluate_schedule(now, latest, _cfg())
    assert decision == "SKIPPED_RECENT_RUN"


# --------------------------------------------------- db integrity

def test_db_integrity_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "BACKEND_DIR", tmp_path)
    ok, detail = orchestrator.check_db_integrity()
    assert ok is False
    assert "no database" in detail


def test_db_integrity_valid_sqlite_file(tmp_path, monkeypatch):
    import sqlite3
    db_path = tmp_path / "esoccer.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE t (id INTEGER)")
    con.commit()
    con.close()
    monkeypatch.setattr(orchestrator, "BACKEND_DIR", tmp_path)
    ok, detail = orchestrator.check_db_integrity()
    assert ok is True


def test_db_integrity_corrupt_file(tmp_path, monkeypatch):
    db_path = tmp_path / "esoccer.db"
    db_path.write_bytes(b"this is not a sqlite database file at all" * 100)
    monkeypatch.setattr(orchestrator, "BACKEND_DIR", tmp_path)
    ok, detail = orchestrator.check_db_integrity()
    assert ok is False


# --------------------------------------------------- credential check

def test_credential_missing_fails_closed(monkeypatch):
    # Ensure app.database (and its load_dotenv() side effect) has already
    # run BEFORE we delete the env vars -- otherwise check_credential()'s own
    # `import app.database` would be a fresh import and re-populate
    # BETSAPI_KEY straight from the real backend/.env, defeating the test.
    import app.database  # noqa: F401
    monkeypatch.delenv("BETSAPI_KEY", raising=False)
    monkeypatch.delenv("BETSAPI_TOKEN", raising=False)
    ok, detail = orchestrator.check_credential()
    assert ok is False
    assert "BETSAPI" in detail
    assert "value" not in detail.split("BETSAPI")[0]  # sanity: no leaked value fragment before the key name


def test_credential_present_passes_and_never_prints_value(monkeypatch):
    monkeypatch.setenv("BETSAPI_KEY", "totally-secret-value-should-never-appear-1234")
    ok, detail = orchestrator.check_credential()
    assert ok is True
    assert "totally-secret-value-should-never-appear-1234" not in detail


# --------------------------------------------------- backend lifecycle (mocked)

def test_ensure_backend_refuses_to_touch_foreign_process(monkeypatch):
    monkeypatch.setattr(orchestrator, "find_port_listener_pid", lambda port=8000: 4242)
    monkeypatch.setattr(orchestrator, "process_command_line", lambda pid: "/usr/bin/some-other-app --serve")
    monkeypatch.setattr(orchestrator, "process_cwd", lambda pid: "/some/other/place")
    killed = []
    monkeypatch.setattr(orchestrator.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    result = orchestrator.ensure_backend()
    assert result["outcome"] == "FOREIGN_PROCESS_ON_PORT"
    assert result["ok"] is False
    assert killed == []  # never signaled the foreign process


def test_ensure_backend_recognizes_ownership_via_cwd_when_cmdline_is_relative(monkeypatch):
    """Reproduces the real bug found during manual validation: a backend
    started with `cd backend && uvicorn ...` (relative paths, exactly what
    run_workday_autopilot.py and a manual restart both do) never contains
    the repo's absolute path in its command line at all -- ownership must
    also be checked via the process's actual cwd."""
    monkeypatch.setattr(orchestrator, "find_port_listener_pid", lambda port=8000: 909)
    monkeypatch.setattr(orchestrator, "process_command_line",
                        lambda pid: ".venv/bin/python3 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000")
    monkeypatch.setattr(orchestrator, "process_cwd", lambda pid: str(orchestrator.BACKEND_DIR))
    monkeypatch.setattr(orchestrator, "backend_health", lambda timeout_s=2.0: {"status": "OK"})
    result = orchestrator.ensure_backend()
    assert result["outcome"] == "REUSED_HEALTHY"


def test_ensure_backend_reuses_healthy_own_backend(monkeypatch):
    monkeypatch.setattr(orchestrator, "find_port_listener_pid", lambda port=8000: 555)
    monkeypatch.setattr(orchestrator, "process_command_line",
                        lambda pid: f"{orchestrator.BACKEND_DIR}/.venv/bin/uvicorn app.main:app")
    monkeypatch.setattr(orchestrator, "backend_health", lambda timeout_s=2.0: {"status": "OK"})
    started = []
    monkeypatch.setattr(orchestrator, "_start_backend_process", lambda: started.append(1))
    result = orchestrator.ensure_backend()
    assert result["outcome"] == "REUSED_HEALTHY"
    assert started == []


def test_ensure_backend_starts_when_absent(monkeypatch):
    monkeypatch.setattr(orchestrator, "find_port_listener_pid", lambda port=8000: None)
    calls = {"health_checks": 0}

    class FakeProc:
        pid = 777

    def fake_start():
        return FakeProc()

    def fake_health(timeout_s=2.0):
        calls["health_checks"] += 1
        return {"status": "OK"} if calls["health_checks"] >= 2 else None

    monkeypatch.setattr(orchestrator, "_start_backend_process", fake_start)
    monkeypatch.setattr(orchestrator, "backend_health", fake_health)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)
    result = orchestrator.ensure_backend()
    assert result["outcome"] == "STARTED"
    assert result["pid"] == 777


# --------------------------------------------------- no input(), no blanket yes

def test_no_input_call_anywhere_in_new_ops_scripts():
    """AST-based, not regex -- these files' own docstrings mention
    'input()' descriptively ('never calls input()'), which a naive text
    search would misflag as an offense."""
    import ast
    offenders = []
    for name in ("run_unattended_workday.py", "unattended_lock.py", "install_daily_launchagent.py",
                "uninstall_daily_launchagent.py", "daily_launchagent_status.py"):
        tree = ast.parse((SCRIPTS_OPS / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input":
                offenders.append(name)
    assert offenders == []


def test_yes_flag_rejected_not_a_blanket_confirm():
    assert orchestrator.main(["--yes"]) == 1


# --------------------------------------------------- LaunchAgent plist

installer = _load("install_daily_launchagent.py", "v37d4_installer")
uninstaller = _load("uninstall_daily_launchagent.py", "v37d4_uninstaller")


def test_plist_template_has_no_environment_variables_block():
    """The template may still mention BETSAPI_KEY descriptively in its own
    explanatory XML comment (documenting WHY there's no env block) -- what
    must never exist is the actual <EnvironmentVariables> plist mechanism or
    a <key>BETSAPI...</key> entry that could carry a real value."""
    text = installer.TEMPLATE_PATH.read_text()
    assert "<key>EnvironmentVariables</key>" not in text
    assert "<key>BETSAPI" not in text


def test_rendered_plist_is_valid_and_secret_free():
    rendered = installer.render_plist(hour=3, minute=30, max_minutes=480)
    installer.validate_plist(rendered)  # must not raise
    installer.confirm_no_secret(rendered)  # must not raise
    assert "<<" not in rendered  # every placeholder was substituted
    assert "03" not in rendered or "<integer>3</integer>" in rendered  # Hour substituted, not corrupted
    assert "<integer>3</integer>" in rendered
    assert "<integer>30</integer>" in rendered
    assert "480" in rendered


def test_plutil_lint_accepts_rendered_plist():
    import shutil
    if not shutil.which("plutil"):
        pytest.skip("plutil not available on this system")
    rendered = installer.render_plist(hour=2, minute=0, max_minutes=480)
    proc = subprocess.run(["plutil", "-lint", "-"], input=rendered, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _fake_subprocess_run_factory(real_run):
    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "launchctl":
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return real_run(cmd, *args, **kwargs)
    return fake_run


def test_installer_refuses_overwrite_without_replace(tmp_path, monkeypatch):
    fake_agents_dir = tmp_path / "LaunchAgents"
    fake_agents_dir.mkdir(parents=True)
    monkeypatch.setattr(installer, "LAUNCH_AGENTS_DIR", fake_agents_dir)
    monkeypatch.setattr(installer, "INSTALLED_PLIST_PATH", fake_agents_dir / f"{installer.LABEL}.plist")
    monkeypatch.setattr(installer, "CONFIG_MARKER_PATH", tmp_path / "launchagent_config.json")
    installer.INSTALLED_PLIST_PATH.write_text("<plist>existing</plist>")

    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run_factory(real_run))
    monkeypatch.setattr(sys, "argv", ["install_daily_launchagent.py"])
    with pytest.raises(SystemExit) as exc:
        installer.main()
    assert "replace" in str(exc.value)


def test_installer_replace_flag_allows_overwrite(tmp_path, monkeypatch):
    fake_agents_dir = tmp_path / "LaunchAgents"
    fake_agents_dir.mkdir(parents=True)
    monkeypatch.setattr(installer, "LAUNCH_AGENTS_DIR", fake_agents_dir)
    monkeypatch.setattr(installer, "INSTALLED_PLIST_PATH", fake_agents_dir / f"{installer.LABEL}.plist")
    monkeypatch.setattr(installer, "CONFIG_MARKER_PATH", tmp_path / "launchagent_config.json")
    installer.INSTALLED_PLIST_PATH.write_text("<plist>existing</plist>")

    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run_factory(real_run))
    monkeypatch.setattr(sys, "argv", ["install_daily_launchagent.py", "--replace"])
    rc = installer.main()
    assert rc == 0
    installed_text = installer.INSTALLED_PLIST_PATH.read_text()
    assert "<key>EnvironmentVariables</key>" not in installed_text
    assert "<key>BETSAPI" not in installed_text


def test_uninstall_preserves_reports_db_backups_logs(tmp_path, monkeypatch):
    fake_agents_dir = tmp_path / "LaunchAgents"
    fake_agents_dir.mkdir(parents=True)
    plist_path = fake_agents_dir / f"{uninstaller.LABEL}.plist"
    plist_path.write_text("<plist>x</plist>")
    monkeypatch.setattr(uninstaller, "LAUNCH_AGENTS_DIR", fake_agents_dir)
    monkeypatch.setattr(uninstaller, "INSTALLED_PLIST_PATH", plist_path)

    other_files = [tmp_path / "report.json", tmp_path / "esoccer.db", tmp_path / "backup.db",
                  tmp_path / "run.log"]
    for f in other_files:
        f.write_text("do not touch")

    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", _fake_subprocess_run_factory(real_run))
    rc = uninstaller.main()
    assert rc == 0
    assert not plist_path.exists()
    for f in other_files:
        assert f.exists() and f.read_text() == "do not touch"
