#!/usr/bin/env python3
"""v0.3.7D Section 9: full-day run readiness check.

Usage:
    python3 scripts/ops/preflight_workday_run.py

Prints PASS / WARN / FAIL per check plus one exact next command. Never
prints secret values (only presence/absence of BETSAPI_KEY/BETSAPI_TOKEN).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def check_upcoming_matches() -> tuple[str, str]:
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Match
    db = SessionLocal()
    try:
        now = _now()
        n = db.scalar(select(Match.id).where(
            Match.start_time > now, Match.start_time < now + timedelta(hours=2),
            Match.ext_id.is_not(None)).limit(1))
        cnt = len(db.scalars(select(Match.id).where(
            Match.start_time > now, Match.start_time < now + timedelta(hours=2),
            Match.ext_id.is_not(None))).all())
        if cnt == 0:
            return "WARN", "no upcoming matches discovered in the next 2 hours yet -- discovery may need a tick"
        return "PASS", f"{cnt} upcoming match(es) discovered in the next 2 hours"
    finally:
        db.close()


def check_caffeinate() -> tuple[str, str]:
    path = shutil.which("caffeinate")
    if path:
        return "PASS", f"caffeinate available at {path}"
    return "WARN", "caffeinate not found -- full unattended runs risk the Mac sleeping"


def main():
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


if __name__ == "__main__":
    main()
