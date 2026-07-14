#!/usr/bin/env python3
"""v0.3.7D.4: fully unattended daily orchestrator.

    python3 scripts/ops/run_unattended_workday.py --non-interactive --allow-warn --max-minutes 480

Orchestrates the three already-tested ops scripts (preflight, autopilot,
daily cycle) as subprocesses -- this file never re-implements their business
logic, only sequences them, adds single-instance locking, backend lifecycle
management, credential validation, catch-up/spacing policy, and evidence
checkpoints around them.

No live betting, no bet placement, no bankroll automation, no model
promotion, no model/prediction/entry/polling-cadence changes happen here or
anywhere this script touches. Never calls input(). Never prints a secret
value.

State machine (see notes/triage/v0_3_7D4-unattended-self-challenge.md):
  1.  Acquire single-instance lock.
  2.  Record run identifier and start timestamp.
  3.  Verify repository/code version (git commit).
  4.  Verify database path and writability (+ integrity check).
  5.  Verify required credential availability without printing it.
  6.  Check whether collector/poller is already active.
  7.  Start or reuse the backend safely.
  8.  Run preflight non-interactively with WARN acceptance.
  9.  Start the 480-minute caffeinated autopilot (wrapped in orchestrator caffeinate).
  10. Wait for it to complete or fail.
  11. Confirm poller self-disabled.
  12. Run the daily cycle non-interactively.
  13. Verify backup creation.
  14. Verify workday/research/simulation/combined-summary outputs.
  15. Write final unattended-run status (+ evidence checkpoints).
  16. Release lock.
  17. Exit with a meaningful status code.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_DIR / "scripts" / "ops"))

from unattended_lock import AlreadyRunning, UnattendedLock  # noqa: E402

STATUS_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/status")
LOCK_PATH = REPO_DIR / "logs" / "unattended" / "run.lock"
UNATTENDED_LOG_DIR = REPO_DIR / "logs" / "unattended"

FINAL_STATUSES = (
    "SUCCESS", "SUCCESS_WITH_WARNINGS", "ALREADY_RUNNING", "SKIPPED_RECENT_RUN",
    "MISSED_WINDOW", "PREFLIGHT_FAILED", "COLLECTION_FAILED", "REPORTING_FAILED",
    "DB_INTEGRITY_FAILURE", "CREDENTIAL_UNAVAILABLE",
)

DEFAULT_SCHEDULED_HOUR = 2
DEFAULT_SCHEDULED_MINUTE = 0
DEFAULT_CATCHUP_HOURS = 6.0
DEFAULT_MIN_HOURS_BETWEEN_RUNS = 18.0

YES_FLAG_REJECTION = ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
                     "FAIL items always stop. Dangerous actions are never auto-confirmed.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utcfromtimestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _log(msg: str) -> None:
    print(f"[{_now().isoformat()}] {msg}")


# ------------------------------------------------------------- step 3: version

def git_commit_hash() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def git_describe() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(REPO_DIR), "describe", "--tags", "--always"],
                          capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ------------------------------------------------------------- step 4: db integrity

def check_db_integrity() -> tuple[bool, str]:
    db_path = BACKEND_DIR / "esoccer.db"
    if not db_path.exists():
        return False, f"no database at {db_path}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            result = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        if result and result[0] == "ok":
            return True, "PRAGMA quick_check: ok"
        return False, f"PRAGMA quick_check reported: {result}"
    except sqlite3.Error as e:
        return False, f"sqlite integrity check raised: {e}"


# ------------------------------------------------------------- step 5: credential

def check_credential() -> tuple[bool, str]:
    """Never prints the value. Loads backend/.env via the same path every
    other ops script uses (import app.database triggers load_dotenv()),
    then only checks presence. Also reports (non-blocking) whether the
    secret file's permissions look world/group-readable."""
    os.chdir(BACKEND_DIR)
    import app.database  # noqa: F401 -- triggers load_dotenv()
    present = bool(os.environ.get("BETSAPI_KEY") or os.environ.get("BETSAPI_TOKEN"))
    if not present:
        return False, "neither BETSAPI_KEY nor BETSAPI_TOKEN is set in backend/.env or the environment"
    env_path = BACKEND_DIR / ".env"
    perm_note = ""
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        if mode & 0o077:
            perm_note = (f" (WARN: backend/.env permissions are {oct(mode)} -- "
                        "group/other-readable; consider `chmod 600 backend/.env`)")
    return True, f"credential present (value not shown){perm_note}"


# ------------------------------------------------------------- steps 6/7: backend lifecycle

def find_port_listener_pid(port: int = 8000) -> int | None:
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                          capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def process_command_line(pid: int) -> str:
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                          capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def process_cwd(pid: int) -> str | None:
    """Belonging-to-this-repo is decided by the process's actual working
    directory, not its command line -- a backend started with a relative
    path (`cd backend && uvicorn ...`, exactly what run_workday_autopilot.py
    and manual runs both do) never contains the repo's absolute path in its
    command line at all."""
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                          capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        pass
    return None


def backend_health(timeout_s: float = 2.0) -> dict | None:
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=timeout_s)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def ensure_backend(bounded_attempts: int = 3) -> dict:
    """Task 3: reuse a healthy backend belonging to THIS repo; gracefully
    replace one that belongs to this repo but is unhealthy; never touch a
    process on port 8000 that does not belong to this repo."""
    pid = find_port_listener_pid(8000)
    if pid is not None:
        cmdline = process_command_line(pid)
        cwd = process_cwd(pid)
        belongs_here = (str(BACKEND_DIR) in cmdline) or (cwd is not None and Path(cwd) == BACKEND_DIR)
        if not belongs_here:
            return {"outcome": "FOREIGN_PROCESS_ON_PORT", "pid": pid, "cmdline": cmdline, "cwd": cwd,
                    "ok": False, "detail": "a process not belonging to this repo is listening on "
                                           "port 8000 -- refusing to touch it"}
        h = backend_health()
        if h is not None:
            return {"outcome": "REUSED_HEALTHY", "pid": pid, "ok": True, "health": h}
        # Ours, but not answering health -- gracefully replace (SIGTERM, wait, restart).
        _log(f"Backend pid={pid} belongs to this repo but is not answering health -- restarting gracefully.")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if find_port_listener_pid(8000) is None:
                break
            time.sleep(0.5)
        pid = None  # fall through to start

    for attempt in range(1, bounded_attempts + 1):
        proc = _start_backend_process()
        for _ in range(30):
            if backend_health() is not None:
                return {"outcome": "STARTED", "pid": proc.pid, "ok": True, "attempts": attempt}
            time.sleep(1)
        _log(f"Backend start attempt {attempt}/{bounded_attempts} did not come up in time.")
    return {"outcome": "FAILED_TO_START", "ok": False, "attempts": bounded_attempts}


def _start_backend_process() -> subprocess.Popen:
    log_path = BACKEND_DIR / "uvicorn.unattended.log"
    log_f = open(log_path, "a")
    cmd = [str(BACKEND_DIR / ".venv" / "bin" / "uvicorn"), "app.main:app",
          "--host", "127.0.0.1", "--port", "8000"]
    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR), stdout=log_f, stderr=log_f)
    _log(f"Started backend pid={proc.pid} (log: {log_path})")
    return proc


# ------------------------------------------------------------- caffeinate lifecycle

class CaffeinateGuard:
    """Task 7: the orchestrator owns caffeinate coverage for the ENTIRE
    active-collection window, independent of run_workday_autopilot.py's own
    --caffeinate flag (which only wraps a freshly-started backend and has no
    effect once the orchestrator has already ensured one is running)."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        path = shutil.which("caffeinate")
        if not path:
            _log("WARN: caffeinate not found -- sleep prevention unavailable for this run.")
            return
        self.proc = subprocess.Popen([path, "-i"])
        _log(f"caffeinate started, pid={self.proc.pid}")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        _log(f"caffeinate pid={self.proc.pid} terminated (no orphan left).")
        self.proc = None


# ------------------------------------------------------------- catch-up / spacing policy

def load_catchup_config() -> dict:
    def _f(name, default):
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
    return {
        "scheduled_hour": int(_f("UNATTENDED_SCHEDULED_HOUR", DEFAULT_SCHEDULED_HOUR)),
        "scheduled_minute": int(_f("UNATTENDED_SCHEDULED_MINUTE", DEFAULT_SCHEDULED_MINUTE)),
        "catch_up_hours": _f("UNATTENDED_CATCHUP_HOURS", DEFAULT_CATCHUP_HOURS),
        "min_hours_between_runs": _f("UNATTENDED_MIN_HOURS_BETWEEN_RUNS", DEFAULT_MIN_HOURS_BETWEEN_RUNS),
    }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def evaluate_schedule(now: datetime, latest_status: dict | None, cfg: dict) -> tuple[str, str]:
    """Returns (decision, reason). decision is one of PROCEED,
    SKIPPED_RECENT_RUN, MISSED_WINDOW. Never uses calendar-day boundaries as
    the only evidence -- always compares against the actual completed-run
    timestamp. Acceptance-test runs never count toward spacing/catch-up
    (Task 13: a 5-minute acceptance run must not block or fake-satisfy a
    real scheduled run)."""
    last_real = None
    if latest_status and not latest_status.get("acceptance_test") and latest_status.get("actual_end"):
        try:
            last_real = datetime.fromisoformat(latest_status["actual_end"])
        except ValueError:
            last_real = None

    if last_real is not None:
        hours_since = (now - last_real).total_seconds() / 3600.0
        if hours_since < cfg["min_hours_between_runs"]:
            return "SKIPPED_RECENT_RUN", (f"last completed unattended run finished {hours_since:.1f}h ago, "
                                          f"below the {cfg['min_hours_between_runs']}h minimum spacing")

    scheduled_today = now.replace(hour=cfg["scheduled_hour"], minute=cfg["scheduled_minute"],
                                  second=0, microsecond=0)
    if now < scheduled_today:
        scheduled_today -= timedelta(days=1)  # still inside yesterday's catch-up window, if any
    window_end = scheduled_today + timedelta(hours=cfg["catch_up_hours"])

    if now <= window_end:
        return "PROCEED", f"inside scheduled/catch-up window (scheduled={scheduled_today.isoformat()})"
    return "MISSED_WINDOW", (f"now={now.isoformat()} is past the catch-up window "
                             f"(scheduled={scheduled_today.isoformat()}, "
                             f"window_end={window_end.isoformat()})")


# ------------------------------------------------------------- subprocess helpers

def _run(cmd: list[str], timeout: int) -> dict:
    proc = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)
    return {"cmd": cmd, "returncode": proc.returncode,
           "stdout_tail": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


# ------------------------------------------------------------- evidence checkpoints

def _build_and_write_checkpoint(run_id: str, phase: str, health: dict | None) -> dict:
    os.chdir(BACKEND_DIR)
    from app.database import SessionLocal
    from app.engines import collection_evidence, evidence_checkpoint
    db = SessionLocal()
    try:
        now = _now()
        evidence = None
        if health is not None:
            evidence = collection_evidence.resolve_collection_evidence(db, health, now)
        checkpoint = evidence_checkpoint.build_checkpoint(db, now, run_id=run_id,
                                                         collection_evidence_result=evidence)
        checkpoint["phase"] = phase
        return checkpoint
    finally:
        db.close()


def _checkpoint_store_path() -> Path:
    return STATUS_DIR / "latest_evidence_checkpoint.json"


def _load_checkpoint_store() -> dict:
    return _read_json(_checkpoint_store_path()) or {"history": []}


def _save_checkpoint(checkpoint: dict, md_name: str) -> dict:
    store = _load_checkpoint_store()
    previous = store["history"][-1] if store["history"] else None
    store["history"] = (store.get("history") or [])[-29:] + [checkpoint]
    store["latest"] = checkpoint
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    _checkpoint_store_path().write_text(json.dumps(store, indent=2, default=str))
    md_path = STATUS_DIR / md_name
    md_path.write_text(f"# Evidence checkpoint ({checkpoint['phase']}) -- {checkpoint['checkpoint_at']}\n\n"
                       f"```json\n{json.dumps(checkpoint, indent=2, default=str)}\n```\n")
    return {"checkpoint": checkpoint, "previous": previous}


# ------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--allow-warn", action="store_true")
    ap.add_argument("--max-minutes", type=int, default=480)
    ap.add_argument("--dry-run", action="store_true",
                    help="run steps 1-8 (lock, version, db, credential, backend, preflight) and report "
                        "what would happen, without starting collection or the daily cycle")
    ap.add_argument("--acceptance-test", action="store_true",
                    help="tag this run so it never counts toward catch-up/spacing decisions for a real run")
    ap.add_argument("--ignore-schedule", action="store_true",
                    help="skip the catch-up/spacing check (used by manual triggers, never by the LaunchAgent)")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.yes:
        print(f"FAIL: {YES_FLAG_REJECTION}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex[:12]
    start_ts = _now()
    _log(f"=== v0.3.7D.4 Unattended Workday Orchestrator (run_id={run_id}) ===")

    accepted_warnings: list[str] = []
    result: dict = {
        "run_id": run_id, "scheduled_hour_minute": None, "actual_start": start_ts.isoformat(),
        "actual_end": None, "acceptance_test": args.acceptance_test, "dry_run": args.dry_run,
        "commit": git_commit_hash(), "version_describe": git_describe(),
        "steps": {}, "final_status": None,
    }

    # ---- step 1/2: schedule + lock -----------------------------------
    cfg = load_catchup_config()
    result["catchup_config"] = cfg
    if not args.dry_run and not args.acceptance_test and not args.ignore_schedule:
        latest = _read_json(STATUS_DIR / "latest_unattended_run.json")
        decision, reason = evaluate_schedule(start_ts, latest, cfg)
        result["schedule_decision"] = {"decision": decision, "reason": reason}
        if decision != "PROCEED":
            result["final_status"] = decision
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log(f"{decision}: {reason}")
            return 0 if decision in ("SKIPPED_RECENT_RUN",) else 3

    UNATTENDED_LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock = UnattendedLock(LOCK_PATH, run_id=run_id, repo_path=str(REPO_DIR))
    try:
        lock.acquire()
    except AlreadyRunning as e:
        result["final_status"] = "ALREADY_RUNNING"
        result["actual_end"] = _now().isoformat()
        result["lock_holder"] = e.holder
        _write_status(result)
        _log(f"ALREADY_RUNNING: {e.holder}")
        return 4

    caffeinate = CaffeinateGuard()

    def _handle_signal(signum, _frame):
        _log(f"Received signal {signum} -- shutting down safely.")
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        # ---- step 6: application-level poller check -------------------
        os.chdir(BACKEND_DIR)
        from app.database import SessionLocal
        from app.models import Settings

        db = SessionLocal()
        try:
            s = db.get(Settings, 1)
            poller_already_on = bool(s and s.poller_enabled)
        finally:
            db.close()
        result["steps"]["poller_precheck"] = {"poller_already_enabled": poller_already_on}
        if poller_already_on and not args.dry_run:
            result["final_status"] = "ALREADY_RUNNING"
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log("ALREADY_RUNNING: Settings.poller_enabled is already true (no lock file, but a live "
                "collection is in progress -- refusing to start a second one).")
            return 4

        # ---- step 4: db integrity --------------------------------------
        db_ok, db_detail = check_db_integrity()
        result["steps"]["db_integrity"] = {"ok": db_ok, "detail": db_detail}
        if not db_ok:
            result["final_status"] = "DB_INTEGRITY_FAILURE"
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log(f"DB_INTEGRITY_FAILURE: {db_detail}")
            return 5

        # ---- step 5: credential -----------------------------------------
        cred_ok, cred_detail = check_credential()
        result["steps"]["credential"] = {"ok": cred_ok, "detail": cred_detail}
        if not cred_ok:
            result["final_status"] = "CREDENTIAL_UNAVAILABLE"
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log(f"CREDENTIAL_UNAVAILABLE: {cred_detail}")
            return 6

        # ---- step 7: backend lifecycle -----------------------------------
        backend_result = ensure_backend()
        result["steps"]["backend"] = backend_result
        if not backend_result["ok"]:
            result["final_status"] = "COLLECTION_FAILED"
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log(f"COLLECTION_FAILED: backend lifecycle failed: {backend_result}")
            return 7
        _log(f"Backend: {backend_result['outcome']} (pid={backend_result.get('pid')})")

        # ---- step 8: preflight --------------------------------------------
        preflight = _run([sys.executable, "scripts/ops/preflight_workday_run.py",
                         "--non-interactive", "--allow-warn"], timeout=120)
        result["steps"]["preflight"] = preflight
        if preflight["returncode"] != 0:
            result["final_status"] = "PREFLIGHT_FAILED"
            result["actual_end"] = _now().isoformat()
            _write_status(result)
            _log("PREFLIGHT_FAILED -- see steps.preflight in the status report.")
            return 8
        for line in preflight["stdout_tail"].splitlines():
            if line.startswith("[WARN]"):
                accepted_warnings.append(line)
        result["accepted_warnings"] = accepted_warnings

        if args.dry_run:
            result["final_status"] = "SUCCESS_WITH_WARNINGS" if accepted_warnings else "SUCCESS"
            result["actual_end"] = _now().isoformat()
            result["dry_run_note"] = "stopped after preflight -- no collection or daily cycle was run"
            _write_status(result)
            _log(f"DRY RUN complete: {result['final_status']}")
            return 0

        # ---- pre-run evidence checkpoint -----------------------------------
        health_before = backend_health()
        try:
            pre_checkpoint = _build_and_write_checkpoint(run_id, "pre_run", health_before)
            saved = _save_checkpoint(pre_checkpoint, f"{start_ts.strftime('%Y-%m-%d')}-pre-run-evidence.md")
            result["pre_run_checkpoint"] = pre_checkpoint
        except Exception as e:
            _log(f"WARN: pre-run evidence checkpoint failed: {e}")
            result["pre_run_checkpoint_error"] = str(e)

        # ---- step 9/10: autopilot -------------------------------------------
        max_minutes = 5 if args.acceptance_test else args.max_minutes
        caffeinate.start()
        autopilot_timeout = max_minutes * 60 + 600  # generous margin over the configured cap
        collection_ok = True
        try:
            autopilot = _run([sys.executable, "scripts/ops/run_workday_autopilot.py",
                             "--max-minutes", str(max_minutes), "--caffeinate",
                             "--non-interactive", "--allow-warn"], timeout=autopilot_timeout)
            result["steps"]["autopilot"] = autopilot
            if autopilot["returncode"] != 0:
                collection_ok = False
        except subprocess.TimeoutExpired as e:
            result["steps"]["autopilot"] = {"error": "timeout", "detail": str(e)}
            collection_ok = False
        finally:
            caffeinate.stop()

        # ---- step 11: confirm poller self-disabled --------------------------
        db = SessionLocal()
        try:
            s = db.get(Settings, 1)
            if s and s.poller_enabled:
                _log("Poller did not self-disable -- clearing it now (orchestrator finalization, "
                    "never a repair of collected data).")
                s.poller_enabled = False
                db.commit()
                collection_ok = False
            result["steps"]["poller_self_disabled"] = {"poller_enabled_after": bool(s and s.poller_enabled)}
        finally:
            db.close()

        if not collection_ok:
            _log("Collection did not complete cleanly -- proceeding to daily cycle for backup/finalization anyway.")

        # ---- step 12: daily cycle (always attempted) -------------------------
        daily_cycle = _run([sys.executable, "scripts/ops/run_daily_cycle.py",
                           "--non-interactive", "--allow-warn"], timeout=1800)
        reporting_ok = daily_cycle["returncode"] == 0
        if not reporting_ok:
            _log("Daily cycle failed -- retrying once (bounded, per Task 8 retry policy).")
            daily_cycle_retry = _run([sys.executable, "scripts/ops/run_daily_cycle.py",
                                     "--non-interactive", "--allow-warn"], timeout=1800)
            reporting_ok = daily_cycle_retry["returncode"] == 0
            result["steps"]["daily_cycle_retry"] = daily_cycle_retry
        result["steps"]["daily_cycle"] = daily_cycle

        # ---- step 13/14: verify outputs ----------------------------------------
        outputs = _verify_outputs(start_ts)
        result["steps"]["output_verification"] = outputs

        # ---- post-run evidence checkpoint + bottleneck classification ---------
        health_after = backend_health()
        try:
            post_checkpoint = _build_and_write_checkpoint(run_id, "post_run", health_after)
            saved = _save_checkpoint(post_checkpoint, f"{start_ts.strftime('%Y-%m-%d')}-post-run-evidence.md")
            result["post_run_checkpoint"] = post_checkpoint
            os.chdir(BACKEND_DIR)
            from app.database import SessionLocal as _SL
            from app.engines import evidence_checkpoint as _ec
            db = _SL()
            try:
                bottleneck = _ec.classify_bottleneck(post_checkpoint, saved.get("previous"))
                thesis = _ec.evaluate_thesis_status(db, post_checkpoint.get("cross_tab_reconciled", False))
                history = _load_checkpoint_store().get("history", [])
                stalled = _ec.check_stalled(history)
            finally:
                db.close()
            result["bottleneck_classification"] = bottleneck
            result["thesis_status"] = thesis
            result["strict_sample_stalled"] = stalled
        except Exception as e:
            _log(f"WARN: post-run evidence checkpoint/classification failed: {e}")
            result["post_run_checkpoint_error"] = str(e)

        result["collection_ok"] = collection_ok
        result["reporting_ok"] = reporting_ok

        if not collection_ok:
            result["final_status"] = "COLLECTION_FAILED"
        elif not reporting_ok:
            result["final_status"] = "REPORTING_FAILED"
        elif accepted_warnings:
            result["final_status"] = "SUCCESS_WITH_WARNINGS"
        else:
            result["final_status"] = "SUCCESS"

        result["actual_end"] = _now().isoformat()
        _write_status(result)
        _log(f"Final status: {result['final_status']}")
        return _exit_code_for(result["final_status"])
    finally:
        lock.release()


def _verify_outputs(since: datetime) -> dict:
    paths = {
        "workday": STATUS_DIR / "latest_workday.json",
        "research": Path("/Users/krispatell/Downloads/ESoccer/notes/research/latest_research.json"),
        "simulation": Path("/Users/krispatell/Downloads/ESoccer/notes/simulations/latest_paper_sim.json"),
        "combined_summary": STATUS_DIR / "latest_daily_cycle.json",
    }
    out = {}
    for name, p in paths.items():
        if not p.exists():
            out[name] = {"ok": False, "detail": "file does not exist"}
            continue
        mtime = _utcfromtimestamp(p.stat().st_mtime)
        out[name] = {"ok": mtime >= since - timedelta(minutes=5), "mtime": mtime.isoformat(), "path": str(p)}
    backups = sorted((BACKEND_DIR / "backups").glob("esoccer-*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    if backups:
        b = backups[0]
        out["backup"] = {"ok": _utcfromtimestamp(b.stat().st_mtime) >= since - timedelta(minutes=5),
                        "path": str(b), "size_bytes": b.stat().st_size}
    else:
        out["backup"] = {"ok": False, "detail": "no backup files found"}
    return out


def _exit_code_for(final_status: str) -> int:
    return 0 if final_status in ("SUCCESS", "SUCCESS_WITH_WARNINGS") else 1


def _write_status(result: dict) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = _now().strftime("%Y-%m-%d")
    (STATUS_DIR / "latest_unattended_run.json").write_text(json.dumps(result, indent=2, default=str))
    md_lines = [
        f"# Unattended Run -- {date_str}", "",
        f"run_id: {result['run_id']}", f"final_status: **{result['final_status']}**",
        f"commit: {result.get('commit')}", f"acceptance_test: {result.get('acceptance_test')}",
        f"actual_start: {result.get('actual_start')}", f"actual_end: {result.get('actual_end')}",
        "", "## Full result", f"```json\n{json.dumps(result, indent=2, default=str)}\n```",
    ]
    (STATUS_DIR / f"{date_str}-unattended-run.md").write_text("\n".join(md_lines))


if __name__ == "__main__":
    sys.exit(main())
