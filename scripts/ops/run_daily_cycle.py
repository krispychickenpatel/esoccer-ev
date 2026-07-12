#!/usr/bin/env python3
"""v0.3.7C: one-command daily run.

    python3 scripts/ops/run_daily_cycle.py

Runs, in order: health precheck -> optional backup -> collection status
check -> daily workday report -> daily research loop -> paper simulation
-> final combined summary. No frontend required. No live betting, no bet
placement, no bankroll automation, no model promotion anywhere in this
chain.

Writes notes/status/YYYY-MM-DD-daily-cycle.md and
notes/status/latest_daily_cycle.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

STATUS_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/status")

# v0.3.7D.1 Task 9: this script never called input() to begin with -- these
# flags exist for a consistent contract across all three ops scripts.
YES_FLAG_REJECTION = ("--yes is not supported. Use --allow-warn to auto-accept WARN-level items. "
                     "FAIL items always stop. Dangerous actions are never auto-confirmed.")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _run_script(rel_path: str, args: list[str] | None = None) -> dict:
    cmd = [sys.executable, str(REPO_DIR / rel_path)] + (args or [])
    proc = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
    return {"script": rel_path, "returncode": proc.returncode,
           "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-2000:]}


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--non-interactive", action="store_true",
                    help="assert non-interactive operation (this script never prompts anyway)")
    ap.add_argument("--allow-warn", action="store_true",
                    help="accepted for contract consistency with the other ops scripts; this script's "
                         "own steps are pass/fail, not pass/warn/fail, so this does not change control flow")
    ap.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.yes:
        print(f"FAIL: {YES_FLAG_REJECTION}", file=sys.stderr)
        sys.exit(1)

    steps = []

    print("1/7 Health precheck...")
    from app.database import SessionLocal
    from app.routers.ops import health
    db = SessionLocal()
    try:
        h = health(db=db)
    finally:
        db.close()
    steps.append({"step": "health_precheck", "status": h["status"], "reason_codes": h["reason_codes"]})
    print(f"    status={h['status']} reason_codes={h['reason_codes']}")

    print("2/7 Backup...")
    backup_result = _run_script("scripts/ops/backup_db.py")
    steps.append({"step": "backup", "ok": backup_result["returncode"] == 0,
                 "output": backup_result["stdout_tail"].strip()})
    print(f"    {'OK' if backup_result['returncode'] == 0 else 'FAILED'}")

    print("3/7 Collection status check...")
    status_result = _run_script("scripts/ops/autopilot_status.py")
    steps.append({"step": "collection_status", "ok": status_result["returncode"] == 0,
                 "output": status_result["stdout_tail"].strip()})
    print(f"    {'OK' if status_result['returncode'] == 0 else 'FAILED'}")

    print("4/7 Daily workday report...")
    workday_result = _run_script("scripts/ops/generate_workday_report.py")
    steps.append({"step": "workday_report", "ok": workday_result["returncode"] == 0,
                 "output": workday_result["stdout_tail"].strip()})
    print(f"    {'OK' if workday_result['returncode'] == 0 else 'FAILED'}")

    print("5/7 Daily research loop...")
    research_result = _run_script("scripts/research/generate_daily_research.py")
    steps.append({"step": "daily_research", "ok": research_result["returncode"] == 0,
                 "output": research_result["stdout_tail"].strip()})
    print(f"    {'OK' if research_result['returncode'] == 0 else 'FAILED'}")

    print("6/7 Paper simulation...")
    sim_result = _run_script("scripts/simulations/run_daily_paper_sim.py")
    steps.append({"step": "paper_simulation", "ok": sim_result["returncode"] == 0,
                 "output": sim_result["stdout_tail"].strip()})
    print(f"    {'OK' if sim_result['returncode'] == 0 else 'FAILED'}")

    print("7/7 Final combined summary...")
    all_ok = all(s.get("ok", True) for s in steps)
    date_str = _now().strftime("%Y-%m-%d")
    summary = {
        "date": date_str, "generated_at": _now().isoformat(),
        "all_steps_ok": all_ok,
        "health_status": h["status"],
        "steps": steps,
        "report_paths": {
            "workday_md": str(STATUS_DIR / f"{date_str}-workday.md"),
            "research_md": str(Path("/Users/krispatell/Downloads/ESoccer/notes/research") /
                              f"{date_str}-daily-research.md"),
            "paper_sim_md": str(Path("/Users/krispatell/Downloads/ESoccer/notes/simulations") /
                              f"{date_str}-paper-sim.md"),
        },
    }

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = STATUS_DIR / f"{date_str}-daily-cycle.md"
    json_path = STATUS_DIR / "latest_daily_cycle.json"
    lines = [f"# Daily Cycle — {date_str}", "", f"Generated: {summary['generated_at']}",
            f"All steps OK: {all_ok}", f"Health status: {h['status']}", "", "## Steps"]
    for s in steps:
        lines.append(f"### {s['step']}")
        lines.append(f"```json\n{json.dumps(s, indent=2)}\n```")
    lines.append("## Report paths")
    lines.append(f"```json\n{json.dumps(summary['report_paths'], indent=2)}\n```")
    md_path.write_text("\n".join(lines))
    json_path.write_text(json.dumps(summary, indent=2, default=str))

    print(f"\nWrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"All steps OK: {all_ok}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
