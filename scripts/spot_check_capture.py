#!/usr/bin/env python3
"""v0.3.7B Section 8: manual book/source spot-check capture.

One-command capture: snapshots the LATEST provider row for a given
match/book/market/side instantly (so the provider-side fields are always
exact, never hand-typed), and lets you fill in the displayed book price
afterward (e.g. from a screen recording) rather than relying on live
typing under time pressure.

Usage:
    python scripts/spot_check_capture.py --match-id 42 --book "FanDuel Ontario" \\
        --sportsbook-provider-key bet365 --market ML_3WAY --side home \\
        --displayed-price 2.10 --notes "checked mid-match"

Do NOT use spot-check rows as model/training data (see hard rules --
this is validation evidence only).
"""
from __future__ import annotations

import argparse
import csv
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
from app.models import Match, OddsSnapshot  # noqa: E402

OUT_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/triage/book_spot_checks.csv")
FIELDS = [
    "capture_id", "captured_at_utc", "local_machine_time", "source", "book", "provider",
    "provider_event_id", "displayed_match", "normalized_match_id", "market", "side", "line",
    "displayed_price", "provider_latest_price", "provider_source_ts", "provider_polled_at",
    "provider_ingested_at", "provider_age_s", "market_available_on_book",
    "market_available_on_provider", "screenshot_or_recording_ref", "notes",
]


def next_capture_id() -> int:
    if not OUT_CSV.exists():
        return 1
    with open(OUT_CSV) as f:
        rows = list(csv.DictReader(f))
    return (max((int(r["capture_id"]) for r in rows), default=0) + 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match-id", type=int, required=True, help="internal Match.id")
    ap.add_argument("--sportsbook-provider-key", default="bet365",
                    help="BetsAPI source key used to poll (default bet365)")
    ap.add_argument("--book", required=True, help="the real book you're comparing against, e.g. 'FanDuel Ontario'")
    ap.add_argument("--market", default="ML_3WAY")
    ap.add_argument("--side", required=True, help="home/draw/away")
    ap.add_argument("--line", default="")
    ap.add_argument("--displayed-price", default="", help="price you saw on the book screen, decimal odds")
    ap.add_argument("--market-available-on-book", default="", help="true/false/unknown")
    ap.add_argument("--source", default="manual", help="manual / hotkey / screen_record_review")
    ap.add_argument("--screenshot-ref", default="")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        match = db.get(Match, args.match_id)
        if match is None:
            print(f"No Match with id={args.match_id}", file=sys.stderr)
            sys.exit(1)
        latest = db.query(OddsSnapshot).filter(
            OddsSnapshot.match_id == args.match_id,
            OddsSnapshot.sportsbook == args.sportsbook_provider_key,
            OddsSnapshot.market == args.market,
            OddsSnapshot.selection == args.side,
        ).order_by(OddsSnapshot.collected_at.desc()).first()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        provider_age_s = None
        if latest is not None:
            provider_age_s = round((now - latest.collected_at).total_seconds(), 1)

        row = {
            "capture_id": next_capture_id(),
            "captured_at_utc": now.isoformat(),
            "local_machine_time": datetime.now().astimezone().isoformat(),
            "source": args.source,
            "book": args.book,
            "provider": "BetsAPI",
            "provider_event_id": match.ext_id or "",
            "displayed_match": f"{match.home_player.name} vs {match.away_player.name}" if match.home_player else "",
            "normalized_match_id": match.id,
            "market": args.market,
            "side": args.side,
            "line": args.line,
            "displayed_price": args.displayed_price,
            "provider_latest_price": latest.decimal_odds if latest else "",
            "provider_source_ts": latest.collected_at.isoformat() if latest else "",
            "provider_polled_at": latest.polled_at.isoformat() if latest and latest.polled_at else "",
            "provider_ingested_at": latest.ingested_at.isoformat() if latest and latest.ingested_at else "",
            "provider_age_s": provider_age_s if provider_age_s is not None else "",
            "market_available_on_book": args.market_available_on_book,
            "market_available_on_provider": "true" if latest is not None else "unknown",
            "screenshot_or_recording_ref": args.screenshot_ref,
            "notes": args.notes,
        }

        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        is_new = not OUT_CSV.exists()
        with open(OUT_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new:
                w.writeheader()
            w.writerow(row)

        print(f"Captured spot-check #{row['capture_id']} -> {OUT_CSV}")
        print(f"  provider_latest_price={row['provider_latest_price']} "
              f"age={row['provider_age_s']}s source_ts={row['provider_source_ts']}")
        print("  Fill in displayed_price/market_available_on_book afterward if left blank "
              "(edit the CSV directly, or re-run with --displayed-price).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
