#!/usr/bin/env python3
"""v0.3.7D.4 Task 9/11: one-command unattended-operations status.

    python3 scripts/ops/daily_launchagent_status.py

Read-only. Never runs collection, the daily cycle, or any subprocess other
than `launchctl print` (to check load state).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
STATUS_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/status")
LABEL = "com.esoccer.daily-unattended"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
INSTALLED_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
CONFIG_MARKER_PATH = REPO_DIR / "logs" / "unattended" / "launchagent_config.json"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utcfromtimestamp(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _launchagent_loaded() -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True)
    return r.returncode == 0


def _backend_health() -> dict | None:
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=2)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _next_scheduled(cfg: dict) -> str:
    now = _now()
    sched = now.replace(hour=cfg.get("hour", 2), minute=cfg.get("minute", 0), second=0, microsecond=0)
    if sched <= now:
        sched += timedelta(days=1)
    return sched.isoformat()


def build_status() -> dict:
    installed = INSTALLED_PLIST_PATH.exists()
    loaded = _launchagent_loaded() if installed else False
    cfg = _read_json(CONFIG_MARKER_PATH) or {}
    last_run = _read_json(STATUS_DIR / "latest_unattended_run.json")
    checkpoint_store = _read_json(STATUS_DIR / "latest_evidence_checkpoint.json") or {}
    latest_checkpoint = checkpoint_store.get("latest")
    health = _backend_health()

    backups_dir = BACKEND_DIR / "backups"
    backups = sorted(backups_dir.glob("esoccer-*.db"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if backups_dir.exists() else []
    last_backup_age_h = (round((_now() - _utcfromtimestamp(backups[0].stat().st_mtime)).total_seconds() / 3600, 1)
                        if backups else None)

    daily_cycle_path = STATUS_DIR / "latest_daily_cycle.json"
    last_report_age_h = (round((_now() - _utcfromtimestamp(daily_cycle_path.stat().st_mtime)).total_seconds() / 3600, 1)
                        if daily_cycle_path.exists() else None)

    strict_45 = (latest_checkpoint or {}).get("lead_gates", {}).get("45s", {})

    return {
        "checked_at": _now().isoformat(),
        "launchagent_installed": installed,
        "launchagent_loaded": loaded,
        "next_scheduled_run": _next_scheduled(cfg) if cfg else None,
        "config": cfg,
        "last_unattended_run": {
            "run_id": (last_run or {}).get("run_id"),
            "final_status": (last_run or {}).get("final_status"),
            "actual_end": (last_run or {}).get("actual_end"),
            "acceptance_test": (last_run or {}).get("acceptance_test"),
            "thesis_status": (last_run or {}).get("thesis_status", {}).get("thesis_status")
                             if isinstance(last_run, dict) and last_run.get("thesis_status") else None,
            "bottleneck_classification": (last_run or {}).get("bottleneck_classification", {}).get("classification")
                                        if isinstance(last_run, dict) and last_run.get("bottleneck_classification") else None,
            "strict_sample_stalled": (last_run or {}).get("strict_sample_stalled"),
        },
        "collector_active": bool(health and health.get("poller_enabled_in_settings")),
        "backend_health_status": (health or {}).get("status", "UNREACHABLE"),
        "last_backup_age_hours": last_backup_age_h,
        "last_report_age_hours": last_report_age_h,
        "strict_45s_clv_n": strict_45.get("strict_executable_forward_clv_n"),
        "strict_45s_progress_to_n50": (min(strict_45.get("strict_executable_forward_clv_n", 0), 50) / 50
                                       if strict_45 else None),
        "strict_45s_progress_to_n150": (min(strict_45.get("strict_executable_forward_clv_n", 0), 150) / 150
                                        if strict_45 else None),
        "next_required_action": (health or {}).get("next_required_action", "backend unreachable"),
    }


def main() -> int:
    out = build_status()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
