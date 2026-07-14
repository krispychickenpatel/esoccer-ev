#!/usr/bin/env python3
"""v0.3.7D.4 Task 5/11: render and install the daily-unattended LaunchAgent.

    python3 scripts/ops/install_daily_launchagent.py \\
      --hour 2 --minute 0 --max-minutes 480 --catch-up-hours 6

Never installs automatically -- this command must be run explicitly. Refuses
to overwrite an existing installation unless --replace is given. Never
embeds a secret value in the rendered plist (see the .plist.template header
and Task 4 in notes/triage/v0_3_7D4-unattended-self-challenge.md).
"""
from __future__ import annotations

import argparse
import getpass
import json
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
TEMPLATE_PATH = REPO_DIR / "scripts" / "ops" / "com.esoccer.daily-unattended.plist.template"
LABEL = "com.esoccer.daily-unattended"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
INSTALLED_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
CONFIG_MARKER_PATH = REPO_DIR / "logs" / "unattended" / "launchagent_config.json"


def render_plist(hour: int, minute: int, max_minutes: int) -> str:
    python3 = BACKEND_DIR / ".venv" / "bin" / "python3"
    if not python3.exists():
        raise SystemExit(f"FAIL: {python3} not found -- run `make setup` first.")
    text = TEMPLATE_PATH.read_text()
    substitutions = {
        "<<PATH_TO_PYTHON3>>": str(python3),
        "<<REPO_DIR>>": str(REPO_DIR),
        "<<MAX_MINUTES>>": str(max_minutes),
        "<<HOUR>>": str(hour),
        "<<MINUTE>>": str(minute),
    }
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    return text


def validate_plist(rendered_text: str) -> None:
    try:
        plistlib.loads(rendered_text.encode())
    except Exception as e:
        raise SystemExit(f"FAIL: rendered plist is not valid XML/plist: {e}")
    # Prefer the real `plutil -lint` when available (Task 13 validation step).
    plutil = shutil.which("plutil")
    if plutil:
        proc = subprocess.run([plutil, "-lint", "-"], input=rendered_text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"FAIL: plutil -lint rejected the rendered plist: {proc.stdout}{proc.stderr}")


def confirm_no_secret(rendered_text: str) -> None:
    """Checks for the actual STRUCTURAL mechanism that could carry a secret
    value (an <EnvironmentVariables> dict, or a <key>BETSAPI...</key> plist
    entry) -- not for the variable NAME appearing in an explanatory XML
    comment, which is expected documentation, not a leak."""
    if "<key>EnvironmentVariables</key>" in rendered_text or "<key>BETSAPI" in rendered_text:
        raise SystemExit("FAIL: rendered plist unexpectedly references credentials -- refusing to install.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hour", type=int, default=2)
    ap.add_argument("--minute", type=int, default=0)
    ap.add_argument("--max-minutes", type=int, default=480)
    ap.add_argument("--catch-up-hours", type=float, default=6.0)
    ap.add_argument("--min-hours-between-runs", type=float, default=18.0)
    ap.add_argument("--replace", action="store_true", help="required to overwrite an existing installation")
    args = ap.parse_args()

    if not (0 <= args.hour <= 23 and 0 <= args.minute <= 59):
        raise SystemExit("FAIL: --hour must be 0-23 and --minute must be 0-59")

    rendered = render_plist(args.hour, args.minute, args.max_minutes)
    validate_plist(rendered)
    confirm_no_secret(rendered)

    print("=== Rendered configuration (no secrets) ===")
    print(f"Label: {LABEL}")
    print(f"Schedule: {args.hour:02d}:{args.minute:02d} local time daily")
    print(f"Max collection minutes: {args.max_minutes}")
    print(f"Catch-up window: {args.catch_up_hours}h")
    print(f"Min hours between completed runs: {args.min_hours_between_runs}h")
    print(f"Repo: {REPO_DIR}")
    print(f"Python: {BACKEND_DIR / '.venv' / 'bin' / 'python3'}")
    print(f"Install path: {INSTALLED_PLIST_PATH}")
    print()

    if INSTALLED_PLIST_PATH.exists() and not args.replace:
        raise SystemExit(f"FAIL: {INSTALLED_PLIST_PATH} already exists -- pass --replace to overwrite it.")

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    if INSTALLED_PLIST_PATH.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True, text=True)

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    INSTALLED_PLIST_PATH.write_text(rendered)
    INSTALLED_PLIST_PATH.chmod(0o644)

    CONFIG_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_MARKER_PATH.write_text(json.dumps({
        "hour": args.hour, "minute": args.minute, "max_minutes": args.max_minutes,
        "catch_up_hours": args.catch_up_hours, "min_hours_between_runs": args.min_hours_between_runs,
        "installed_by": getpass.getuser(),
    }, indent=2))

    result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(INSTALLED_PLIST_PATH)],
                           capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAIL: launchctl bootstrap failed: {result.stdout}{result.stderr}", file=sys.stderr)
        return 1

    check = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True)
    if check.returncode != 0:
        print(f"FAIL: launchctl bootstrap reported success but the agent is not queryable: "
             f"{check.stdout}{check.stderr}", file=sys.stderr)
        return 1

    print(f"OK: {LABEL} installed and loaded.")
    print(f"Next scheduled run: {args.hour:02d}:{args.minute:02d} local time (today or tomorrow, whichever is next).")
    print("Status: python3 scripts/ops/daily_launchagent_status.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
