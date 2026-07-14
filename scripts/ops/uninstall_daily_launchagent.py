#!/usr/bin/env python3
"""v0.3.7D.4 Task 5/11: safely uninstall the daily-unattended LaunchAgent.

    python3 scripts/ops/uninstall_daily_launchagent.py

Unloads and removes the plist only. Never touches reports, the database,
backups, or logs -- those require a separate, explicit cleanup command
(none exists in this release; nothing here deletes research/operational
history).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LABEL = "com.esoccer.daily-unattended"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
INSTALLED_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"


def main() -> int:
    if not INSTALLED_PLIST_PATH.exists():
        print(f"OK: {INSTALLED_PLIST_PATH} does not exist -- nothing to uninstall.")
        return 0

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, text=True)
    if result.returncode != 0 and "Could not find" not in (result.stderr or ""):
        print(f"WARN: launchctl bootout reported: {result.stdout}{result.stderr}")

    INSTALLED_PLIST_PATH.unlink()
    print(f"OK: {INSTALLED_PLIST_PATH} unloaded and removed.")
    print("Reports, database, backups, and logs were NOT touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
