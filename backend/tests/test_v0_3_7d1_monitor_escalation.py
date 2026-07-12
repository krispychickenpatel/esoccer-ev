"""v0.3.7D.1 Task 9/10/12: --yes rejection on the other two ops scripts, and
the monitor's HTTP-timeout escalation (1st WARN, 2nd DEGRADED, 3rd+ FAIL)."""
import importlib.util
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


autopilot = _load("ops/run_workday_autopilot.py", "v37d1_autopilot_test")
daily_cycle = _load("ops/run_daily_cycle.py", "v37d1_daily_cycle_test")

EXACT_REJECTION = ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
                   "FAIL items always stop. Dangerous actions are never auto-confirmed.")


def test_autopilot_rejects_yes_flag(capsys, monkeypatch):
    import pytest
    monkeypatch.setattr(sys, "argv", ["run_workday_autopilot.py", "--max-minutes", "10", "--yes"])
    with pytest.raises(SystemExit) as exc:
        autopilot.main()
    assert exc.value.code == 1
    assert EXACT_REJECTION in capsys.readouterr().err


def test_daily_cycle_rejects_yes_flag(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        daily_cycle.main(["--yes"])
    assert exc.value.code == 1
    assert EXACT_REJECTION in capsys.readouterr().err


def test_classify_http_failure_escalates_then_stays_fail():
    assert autopilot.classify_http_failure(1) == ("WARN", "MONITOR_HTTP_TIMEOUT_TRANSIENT")
    assert autopilot.classify_http_failure(2) == ("DEGRADED", "MONITOR_HTTP_TIMEOUT_REPEATED")
    assert autopilot.classify_http_failure(3) == ("FAIL", "MONITOR_HTTP_ERROR")
    assert autopilot.classify_http_failure(4) == ("FAIL", "MONITOR_HTTP_ERROR")
