#!/usr/bin/env python3
"""v0.3.7C: read-only autopilot/collector status check.

Usage:
    python3 scripts/ops/autopilot_status.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)


def _fetch_health_http(timeout_s: float = 2.0) -> dict | None:
    """Prefer the real backend's own HTTP endpoint -- see the v0.3.7C bug
    note in run_workday_autopilot.py: STATUS (services/poller.py) is
    per-process, so an in-process health() call in THIS script's own
    process would always see a never-started collector, regardless of
    whether the real backend is healthy. Returns None if unreachable
    (backend not running, or this is a test with no real server)."""
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=timeout_s)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_status(db) -> dict:
    """Core logic, callable from tests with any db session (including one
    with no Settings row at all -- handled safely, never raises)."""
    from app.models import Settings

    s = db.get(Settings, 1)
    if s is None:
        return {"ok": False, "error": "no Settings row -- start the backend once first."}

    elapsed_min = None
    if s.autopilot_started_at:
        elapsed_min = round((datetime.now(timezone.utc).replace(tzinfo=None)
                             - s.autopilot_started_at).total_seconds() / 60.0, 1)

    h = _fetch_health_http()
    health_source = "http (real backend)"
    if h is None:
        from app.routers.ops import health
        h = health(db=db)
        health_source = "in-process (no backend reachable at 127.0.0.1:8000 -- may be inaccurate " \
                        "if a different process is actually running the collector)"

    return {
        "ok": True,
        "poller_enabled": s.poller_enabled,
        "autopilot_started_at": s.autopilot_started_at.isoformat() if s.autopilot_started_at else None,
        "autopilot_max_runtime_minutes": s.autopilot_max_runtime_minutes,
        "elapsed_minutes": elapsed_min,
        "minutes_remaining": (round(s.autopilot_max_runtime_minutes - elapsed_min, 1)
                             if (s.autopilot_max_runtime_minutes and elapsed_min is not None) else None),
        "densified_polling_enabled_in_settings": s.densified_polling_enabled,
        "health_status": h["status"],
        "reason_codes": h["reason_codes"],
        "next_required_action": h["next_required_action"],
        "health_source": health_source,
    }


def main():
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        out = get_status(db)
        print(json.dumps(out, indent=2))
        if not out["ok"]:
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
