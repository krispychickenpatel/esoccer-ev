"""v0.3.7D.5 reliability hotfix Task 3: immutable per-invocation status
records. Regression coverage for the exact failure mode that erased the
true record of the 2026-07-15/17 sleep-hang incident: two invocations
completing on the same UTC calendar date used to share ONE overwriteable
file, so the second one's write silently destroyed the first one's record.

All tests here use monkeypatched STATUS_DIR/RUN_RECORDS_DIR pointing at
tmp_path -- never the real notes/ tree."""
import importlib.util
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_OPS = REPO_DIR / "scripts" / "ops"


def _load(rel_name: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_OPS / rel_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


orchestrator = _load("run_unattended_workday.py", "v37d5_history_orchestrator")


def _patch_dirs(monkeypatch, tmp_path):
    status_dir = tmp_path / "status"
    records_dir = status_dir / "unattended_runs"
    monkeypatch.setattr(orchestrator, "STATUS_DIR", status_dir)
    monkeypatch.setattr(orchestrator, "RUN_RECORDS_DIR", records_dir)
    return status_dir, records_dir


def _result(run_id, actual_start, final_status="SUCCESS", **extra):
    r = {"run_id": run_id, "actual_start": actual_start, "actual_end": actual_start,
        "final_status": final_status, "commit": "abc123", "acceptance_test": False,
        "steps": {}}
    r.update(extra)
    return r


def test_two_runs_same_date_both_get_immutable_records(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)

    r1 = _result("run-one", "2026-07-17T02:21:57", final_status="COLLECTION_FAILED")
    r2 = _result("run-two", "2026-07-17T06:00:04", final_status="SKIPPED_RECENT_RUN")

    orchestrator._write_status(r1)
    orchestrator._write_status(r2)

    rec1 = json.loads((records_dir / "run-one.json").read_text())
    rec2 = json.loads((records_dir / "run-two.json").read_text())
    assert rec1["final_status"] == "COLLECTION_FAILED"
    assert rec2["final_status"] == "SKIPPED_RECENT_RUN"
    # the critical regression check: writing the second run's record must
    # NOT have altered or removed the first one.
    assert rec1["run_id"] == "run-one"


def test_latest_pointer_reflects_most_recent_write_only(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    orchestrator._write_status(_result("run-a", "2026-07-17T02:00:00"))
    orchestrator._write_status(_result("run-b", "2026-07-17T10:00:00"))
    latest = json.loads((status_dir / "latest_unattended_run.json").read_text())
    assert latest["run_id"] == "run-b"
    # but run-a's own immutable record must still exist untouched
    assert (records_dir / "run-a.json").exists()


def test_daily_summary_lists_every_invocation_that_date(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    orchestrator._write_status(_result("r1", "2026-07-17T02:21:57", final_status="COLLECTION_FAILED"))
    orchestrator._write_status(_result("r2", "2026-07-17T06:00:04", final_status="SKIPPED_RECENT_RUN"))
    orchestrator._write_status(_result("r3", "2026-07-17T22:20:34", final_status="SUCCESS"))
    orchestrator._write_status(_result("r4", "2026-07-18T02:00:04", final_status="SUCCESS"))

    summary_17 = (status_dir / "2026-07-17-unattended-runs-summary.md").read_text()
    for run_id in ("r1", "r2", "r3"):
        assert run_id in summary_17
    assert "r4" not in summary_17

    summary_18 = (status_dir / "2026-07-18-unattended-runs-summary.md").read_text()
    assert "r4" in summary_18
    assert "3 invocation(s)" in summary_17
    assert "1 invocation(s)" in summary_18


def test_started_record_written_immediately_survives_no_further_writes(tmp_path, monkeypatch):
    """Simulates an abrupt kill: only the initial STARTED write happens,
    _write_status() (the finalization path) is never called. The record
    must still exist and be readable, distinguishing 'started but never
    finished' from 'never happened at all'."""
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    started = {"run_id": "interrupted-run", "actual_start": "2026-07-15T02:00:04",
              "actual_end": None, "final_status": None, "status_phase": "STARTED",
              "acceptance_test": False, "commit": "abc", "steps": {}}
    orchestrator._write_run_record(started)

    rec = json.loads((records_dir / "interrupted-run.json").read_text())
    assert rec["status_phase"] == "STARTED"
    assert rec["final_status"] is None


def test_write_status_upgrades_status_phase_to_completed(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    result = {"run_id": "r-complete", "actual_start": "2026-07-17T02:00:00",
             "actual_end": "2026-07-17T02:05:00", "final_status": "SUCCESS",
             "status_phase": "STARTED", "acceptance_test": False, "commit": "abc", "steps": {}}
    orchestrator._write_status(result)
    rec = json.loads((records_dir / "r-complete.json").read_text())
    assert rec["status_phase"] == "COMPLETED"
    assert rec["final_status"] == "SUCCESS"


def test_same_run_id_record_can_be_updated_in_place(tmp_path, monkeypatch):
    """Not a violation of immutability -- a run updating ITS OWN record
    from STARTED to a final status is expected and safe; only a DIFFERENT
    run_id must never touch another's file."""
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    run_id = "same-run"
    orchestrator._write_run_record({"run_id": run_id, "actual_start": "2026-07-17T02:00:00",
                                    "status_phase": "STARTED", "final_status": None,
                                    "acceptance_test": False, "commit": "abc", "steps": {}})
    orchestrator._write_status({"run_id": run_id, "actual_start": "2026-07-17T02:00:00",
                                "actual_end": "2026-07-17T02:10:00", "final_status": "SUCCESS",
                                "acceptance_test": False, "commit": "abc", "steps": {}})
    files = list(records_dir.glob("*.json"))
    assert len(files) == 1  # still one file for this run_id, now finalized
    rec = json.loads(files[0].read_text())
    assert rec["final_status"] == "SUCCESS"


def test_interrupted_run_detected_by_status_scanner(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    status_mod = _load("daily_launchagent_status.py", "v37d5_status_for_history_test")
    monkeypatch.setattr(status_mod, "STATUS_DIR", status_dir)
    monkeypatch.setattr(status_mod, "RUN_RECORDS_DIR", records_dir)

    from datetime import datetime
    old_start = (datetime(2026, 7, 17, 2, 0, 0)).isoformat()
    orchestrator._write_run_record({"run_id": "stuck-run", "actual_start": old_start,
                                    "status_phase": "STARTED", "final_status": None,
                                    "acceptance_test": False, "commit": "abc", "steps": {}})

    now = datetime(2026, 7, 18, 0, 0, 0)  # 22h after start -- well past the 10h bound
    interrupted = status_mod.scan_interrupted_runs(records_dir, now)
    assert len(interrupted) == 1
    assert interrupted[0]["run_id"] == "stuck-run"


def test_recently_started_run_not_flagged_as_interrupted(tmp_path, monkeypatch):
    status_dir, records_dir = _patch_dirs(monkeypatch, tmp_path)
    status_mod = _load("daily_launchagent_status.py", "v37d5_status_for_history_test2")
    monkeypatch.setattr(status_mod, "STATUS_DIR", status_dir)
    monkeypatch.setattr(status_mod, "RUN_RECORDS_DIR", records_dir)

    from datetime import datetime
    recent_start = datetime(2026, 7, 17, 2, 0, 0).isoformat()
    orchestrator._write_run_record({"run_id": "still-running", "actual_start": recent_start,
                                    "status_phase": "STARTED", "final_status": None,
                                    "acceptance_test": False, "commit": "abc", "steps": {}})
    now = datetime(2026, 7, 17, 4, 0, 0)  # only 2h later -- plausibly still running
    interrupted = status_mod.scan_interrupted_runs(records_dir, now)
    assert interrupted == []
