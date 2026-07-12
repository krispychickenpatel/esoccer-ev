#!/usr/bin/env python3
"""v0.3.7D Section 9: full-day run readiness check.

Usage:
    python3 scripts/ops/preflight_workday_run.py
    python3 scripts/ops/preflight_workday_run.py --non-interactive --allow-warn

Prints PASS / WARN / FAIL per check plus one exact next command. Never
prints secret values (only presence/absence of BETSAPI_KEY/BETSAPI_TOKEN).

v0.3.7D.1 Task 9: safe non-interactive mode. This script never called
input() to begin with, so --non-interactive changes nothing about control
flow -- it exists so the flag can be asserted/logged consistently across
all three ops scripts, and so a caller can rely on `--non-interactive`
being accepted here without checking which of the three scripts it is.
Exit codes: FAIL always non-zero. WARN is non-zero UNLESS --allow-warn is
given, in which case it continues (exit 0) and every accepted warning is
appended to logs/workday/accepted_warnings.jsonl. `--yes` is rejected
outright -- there is no blanket auto-confirm in this codebase.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

ACCEPTED_WARNINGS_LOG = REPO_DIR / "logs" / "workday" / "accepted_warnings.jsonl"

YES_FLAG_REJECTION = ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
                     "FAIL items always stop. Dangerous actions are never auto-confirmed.")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_accepted_warning(run_id: str, code: str, text: str, command: str):
    ACCEPTED_WARNINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": _now().isoformat(), "code": code, "text": text,
            "command": command, "run_id": run_id}
    with open(ACCEPTED_WARNINGS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def attempt_discovery_tick() -> dict:
    """v0.3.7D.1 Task 9: one safe, bounded, synchronous discovery attempt --
    mirrors poll_loop's own discovery step (app/services/poller.py) exactly,
    run once instead of waiting for the background poller's 60s throttle.
    Only upserts newly-discovered upcoming/inplay matches; never touches
    odds polling, predictions, or any decision logic."""
    from app.database import SessionLocal
    from app.models import Settings
    from app.routers.data import upsert_match
    db = SessionLocal()
    try:
        s = db.get(Settings, 1)
        tracked = json.loads(s.tracked_leagues or "[]") if s else []
        if not tracked:
            return {"attempted": False, "reason": "tracked_leagues is empty"}
        from app.connectors.betsapi_provider import BetsApiProvider
        provider = BetsApiProvider(db)
        upcoming = provider.fetch_upcoming()
        if hasattr(provider, "fetch_inplay"):
            upcoming = upcoming + provider.fetch_inplay()
        by_ext = {e.get("ext_id") or f"{e.get('start_time')}-{e.get('home_player')}-{e.get('away_player')}": e
                 for e in upcoming}
        upcoming = list(by_ext.values())
        scoped = [e for e in upcoming if any(t.lower() in (e.get("league") or "").lower() for t in tracked)]
        new_count = 0
        for ev in scoped:
            _, created = upsert_match(db, ev)
            new_count += created
        db.commit()
        return {"attempted": True, "found": len(scoped), "new": new_count}
    except Exception as e:
        return {"attempted": True, "error": str(e)}
    finally:
        db.close()


def check_backend_reachable() -> tuple[str, str]:
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/health", timeout=2)
        if r.status_code == 200:
            return "PASS", "backend reachable on 127.0.0.1:8000"
        return "WARN", f"backend responded with status {r.status_code}"
    except Exception:
        return "WARN", "backend not reachable -- run_workday_autopilot.py will start one"


def check_api_key() -> tuple[str, str]:
    import app.database  # noqa: F401  -- triggers load_dotenv()
    if os.environ.get("BETSAPI_KEY") or os.environ.get("BETSAPI_TOKEN"):
        return "PASS", "BETSAPI_KEY/BETSAPI_TOKEN is set (value not shown)"
    return "FAIL", "neither BETSAPI_KEY nor BETSAPI_TOKEN is set"


def check_poller_settings() -> tuple[str, str]:
    from app.database import SessionLocal
    from app.models import Settings
    db = SessionLocal()
    try:
        s = db.get(Settings, 1)
        if s is None:
            return "FAIL", "no Settings row -- start the backend once first"
        if not s.tracked_leagues or s.tracked_leagues == "[]":
            return "WARN", "tracked_leagues is empty -- poller will idle with nothing to track"
        if not s.sportsbooks_tracked or s.sportsbooks_tracked == "[]":
            return "WARN", "sportsbooks_tracked is empty"
        return "PASS", f"tracked_leagues and sportsbooks_tracked configured; poller_enabled={s.poller_enabled}"
    finally:
        db.close()


def check_db_writable() -> tuple[str, str]:
    from app.routers.ops import _db_writable
    return ("PASS", "database writable") if _db_writable() else ("FAIL", "database is not writable")


def check_disk_headroom() -> tuple[str, str]:
    from app.workday_config import load_workday_config
    cfg = load_workday_config()
    usage = shutil.disk_usage(BACKEND_DIR)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < cfg.min_disk_headroom_mb:
        return "FAIL", f"only {round(free_mb)}MB free, below WORKDAY_MIN_DISK_HEADROOM_MB={cfg.min_disk_headroom_mb}"
    return "PASS", f"{round(free_mb)}MB free"


def check_backup_path_safe() -> tuple[str, str]:
    # Inlined rather than importing scripts/ops/backup_db.py -- `scripts/`
    # is a plain directory, not an importable package, from this cwd.
    d = Path(os.environ.get("WORKDAY_BACKUP_DIR") or (BACKEND_DIR / "backups"))
    gitignore = (REPO_DIR / ".gitignore").read_text()
    if "backups/" not in gitignore and "*.db" not in gitignore:
        return "WARN", f"backup dir {d} may not be git-ignored -- check .gitignore"
    return "PASS", f"backup dir {d} is git-ignored"


def check_collection_window() -> tuple[str, str]:
    from app.workday_config import load_workday_config
    cfg = load_workday_config()
    now = _now()
    active = cfg.in_collection_window(now)
    if cfg.collection_start is None:
        return "PASS", "no WORKDAY_COLLECTION_START/END configured -- always active"
    return ("PASS" if active else "WARN",
           f"now={now.isoformat()} UTC, window_active={active} (WORKDAY_TIMEZONE={cfg.timezone})")


def _count_upcoming_matches() -> int:
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Match
    db = SessionLocal()
    try:
        now = _now()
        return len(db.scalars(select(Match.id).where(
            Match.start_time > now, Match.start_time < now + timedelta(hours=2),
            Match.ext_id.is_not(None))).all())
    finally:
        db.close()


def check_upcoming_matches(retry_with_discovery_tick: bool = True) -> tuple[str, str]:
    """v0.3.7D.1 Task 9: zero upcoming matches no longer WARNs immediately --
    attempt one safe discovery tick first, re-check, and only WARN if still
    empty after the retry. Never fabricates matches; the tick is a no-op if
    tracked_leagues is empty or the provider returns nothing."""
    cnt = _count_upcoming_matches()
    if cnt > 0:
        return "PASS", f"{cnt} upcoming match(es) discovered in the next 2 hours"
    if not retry_with_discovery_tick:
        return "WARN", "no upcoming matches discovered in the next 2 hours yet -- discovery may need a tick"
    tick = attempt_discovery_tick()
    cnt = _count_upcoming_matches()
    if cnt > 0:
        return "PASS", f"{cnt} upcoming match(es) discovered in the next 2 hours (after one discovery-tick retry: {tick})"
    return ("WARN", "no upcoming matches discovered in the next 2 hours, even after one discovery-tick retry "
                    f"({tick}) -- if this persists, confirm tracked_leagues/BETSAPI_KEY and that matches are "
                    "actually scheduled soon.")


def check_caffeinate() -> tuple[str, str]:
    path = shutil.which("caffeinate")
    if path:
        return "PASS", f"caffeinate available at {path}"
    return "WARN", "caffeinate not found -- full unattended runs risk the Mac sleeping"


def run_checks() -> dict:
    checks = [
        ("backend_reachable", check_backend_reachable),
        ("api_key_present", check_api_key),
        ("poller_settings", check_poller_settings),
        ("db_writable", check_db_writable),
        ("disk_headroom", check_disk_headroom),
        ("backup_path_safe", check_backup_path_safe),
        ("collection_window", check_collection_window),
        ("upcoming_matches", check_upcoming_matches),
        ("caffeinate_available", check_caffeinate),
    ]
    results = {}
    worst = "PASS"
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    for name, fn in checks:
        try:
            level, detail = fn()
        except Exception as e:
            level, detail = "FAIL", f"check raised: {e}"
        results[name] = {"level": level, "detail": detail}
        if order[level] > order[worst]:
            worst = level
        print(f"[{level}] {name}: {detail}")

    if worst == "FAIL":
        next_cmd = "Fix the FAIL item(s) above before starting a workday run."
    elif worst == "WARN":
        next_cmd = ("Review WARN item(s) above, then: python3 scripts/ops/run_workday_autopilot.py "
                   "--max-minutes 480 --caffeinate")
    else:
        next_cmd = "python3 scripts/ops/run_workday_autopilot.py --max-minutes 480 --caffeinate"

    print()
    print(f"Overall: {worst}")
    print(f"Next command: {next_cmd}")
    return {"overall": worst, "checks": results, "next_command": next_cmd}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--non-interactive", action="store_true",
                    help="assert non-interactive operation (this script never prompts anyway)")
    ap.add_argument("--allow-warn", action="store_true",
                    help="continue (exit 0) when the worst result is WARN, logging every accepted warning")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.yes:
        print(f"FAIL: {YES_FLAG_REJECTION}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex[:12]
    result = run_checks()

    if args.allow_warn:
        for name, r in result["checks"].items():
            if r["level"] == "WARN":
                _log_accepted_warning(run_id, name, r["detail"],
                                      "scripts/ops/preflight_workday_run.py")

    if result["overall"] == "FAIL":
        return 1
    if result["overall"] == "WARN" and not args.allow_warn:
        print()
        print("Overall is WARN and --allow-warn was not given -- stopping. "
             "Re-run with --allow-warn to auto-accept WARN-level items, or fix them first.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
