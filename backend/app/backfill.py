"""One-time backfill: pull real match history for players active in your
tracked leagues, using BetsAPI's event/history endpoint (per-matchup, not a
bulk league dump). Not run automatically -- trigger manually when you want
fresh history.

    cd backend && python -m app.backfill

Scope: all 5 leagues in Settings.tracked_leagues by default (matches the
"independent model, beat him" instruction -- D23). Cost: ~1 event/history
call per upcoming match sampled (default: first 40 upcoming matches across
tracked leagues), not per-player, since a match's history call returns both
teams' priors in one request.

[Certain] event/history response schema (results.h2h/home/away) is UNVERIFIED
-- BetsAPI's docs didn't publish event_history.json's actual structure. This
script's fetch_event_history() will simply return [] if the guessed keys are
wrong, and log a raw response for you to check. It won't crash or silently
fabricate data either way.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import select

from .connectors.betsapi_provider import BetsApiProvider
from .database import SessionLocal, init_db
from .engines.ratings import rebuild_ratings
from .models import Match, Settings
from .routers.data import upsert_match


def run(sample_size: int = 40) -> dict:
    init_db()
    db = SessionLocal()
    stats = {"events_sampled": 0, "history_calls": 0, "matches_created": 0,
            "matches_updated": 0, "empty_responses": 0}
    try:
        s = db.get(Settings, 1)
        tracked = json.loads(s.tracked_leagues or "[]")
        if not tracked:
            print("Settings.tracked_leagues is empty -- nothing to backfill. "
                  "Set leagues in Settings first.")
            return stats

        provider = BetsApiProvider(db)
        if not provider.token:
            print("BETSAPI_KEY not set in .env -- cannot backfill without a live key.")
            return stats

        upcoming = provider.fetch_upcoming()
        scoped = [e for e in upcoming
                  if any(t.lower() in (e.get("league") or "").lower() for t in tracked)]
        sample = scoped[:sample_size]
        stats["events_sampled"] = len(sample)
        print(f"Sampling {len(sample)} upcoming events across {len(tracked)} tracked leagues...")

        for ev in sample:
            hist = provider.fetch_event_history(ev["ext_id"], qty=20)
            stats["history_calls"] += 1
            if not hist:
                stats["empty_responses"] += 1
                continue
            for row in hist:
                _, created = upsert_match(db, row)
                stats["matches_created"] += created
                stats["matches_updated"] += not created
            db.commit()
            time.sleep(0.05)  # gentle pacing, well under the 3600/hr cap regardless

        if stats["matches_created"] or stats["matches_updated"]:
            rebuild_ratings(db)
            print("Ratings rebuilt from backfilled history.")

        if stats["empty_responses"] == stats["history_calls"] and stats["history_calls"] > 0:
            print("WARNING: every event/history call returned empty. The guessed "
                  "response schema is likely wrong -- check raw_provider_responses "
                  "table for a real payload and fix fetch_event_history() in "
                  "connectors/betsapi_provider.py before assuming there's no history.")

        print(stats)
        return stats
    finally:
        db.close()


if __name__ == "__main__":
    run()
