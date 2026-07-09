#!/usr/bin/env python3
"""v0.3.7C: timestamped local DB backup.

Default backup directory (backend/backups/) is git-ignored -- never commit
a DB backup. An optional private destination can be set via
WORKDAY_BACKUP_DIR (e.g. an external drive or a directory outside the repo
entirely) if you want backups to live somewhere git can never see them by
construction.

Usage:
    python3 scripts/ops/backup_db.py
    python3 scripts/ops/backup_db.py --keep 10
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
DB_PATH = BACKEND_DIR / "esoccer.db"
DEFAULT_BACKUP_DIR = BACKEND_DIR / "backups"


def backup_dir() -> Path:
    override = os.environ.get("WORKDAY_BACKUP_DIR")
    return Path(override) if override else DEFAULT_BACKUP_DIR


def run_backup(db_path: Path, out_dir: Path, keep: int) -> dict:
    """Pure-ish core logic, callable from tests with a temp db_path/out_dir.
    Never touches anything outside out_dir for retention deletion."""
    if not db_path.exists():
        return {"ok": False, "error": f"no database at {db_path} -- nothing to back up."}

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = out_dir / f"esoccer-{stamp}.db"
    shutil.copy2(db_path, dest)

    size = dest.stat().st_size
    if size == 0:
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": f"backup at {dest} was 0 bytes -- removed."}

    existing = sorted(out_dir.glob("esoccer-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in existing[keep:]:
        old.unlink()
        removed.append(str(old))

    return {"ok": True, "path": str(dest), "size_bytes": size, "removed": removed}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", type=int, default=14, help="retain this many most-recent backups")
    args = ap.parse_args()

    result = run_backup(DB_PATH, backup_dir(), args.keep)
    if not result["ok"]:
        print(f"FAIL: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: backup created at {result['path']} ({result['size_bytes']} bytes)")
    if result["removed"]:
        print(f"Retention: removed {len(result['removed'])} old backup(s): {result['removed']}")
    print(f"Backup directory: {backup_dir()} (git-ignored -- never staged)")


if __name__ == "__main__":
    main()
