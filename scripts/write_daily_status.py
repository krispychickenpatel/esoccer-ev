#!/usr/bin/env python3
"""v0.3.7B Section 10: daily status file writer. Reads DB directly (no
HTTP dependency, so it works even if the API process isn't reachable) and
writes notes/status/YYYY-MM-DD.json."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
# app.database's DATABASE_URL defaults to a path relative to the process cwd
# (sqlite:///./esoccer.db) -- must run as if invoked from backend/, same
# convention as the rest of this codebase (see CLAUDE.md).
os.chdir(BACKEND_DIR)

from app.database import SessionLocal  # noqa: E402
from app.routers.ops import health  # noqa: E402

OUT_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/status")


def main():
    db = SessionLocal()
    try:
        result = health(db=db)
    finally:
        db.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = OUT_DIR / f"{today}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"Wrote {out_path}")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
