"""Book Coverage Scanner (v0.3.6 Module 3).

Purpose: find which sportsbooks/apps actually carry these ESoccer
leagues/markets, proven with real non-empty payloads -- never guessed.

Completely separate from the hot poller (services/poller.py). Refuses to
run while any tracked match is inside its live window (KO-2min..KO+2min) so
it can never compete with first-live capture, and is hard-capped at
MAX_CALLS_PER_SCAN real API calls.

bet365 is the permanent reference feed and is never marked execution_candidate,
regardless of its own scan results. Other books only become execution
candidates once a scan proves a non-empty esoccer odds response.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BookmakerCoverage, Match

REFERENCE_BOOK = "bet365"
DEFAULT_SCAN_BOOKS = ["bet365", "fanduel"]
MAX_CALLS_PER_SCAN = 40
SAMPLE_MATCHES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_live_window_active(db: Session) -> bool:
    """True if any not-yet-finished match is within +/-2 minutes of kickoff --
    the scanner must not compete with first-live capture during this window."""
    now = _now()
    n = db.scalar(select(func.count(Match.id)).where(
        Match.home_score.is_(None),
        Match.start_time > now - timedelta(minutes=2),
        Match.start_time < now + timedelta(minutes=2),
    ))
    return bool(n)


def run_scan(db: Session, provider, books: list[str] | None = None,
            max_calls: int = MAX_CALLS_PER_SCAN) -> dict:
    if is_live_window_active(db):
        return {"skipped": True, "reason": "a tracked match is inside its live window "
                "(KO-2min..KO+2min) -- scanner refuses to run to protect first-live capture"}

    books = books or DEFAULT_SCAN_BOOKS
    sample = db.scalars(select(Match).where(
        Match.ext_id.is_not(None), Match.home_score.is_(None),
    ).order_by(Match.start_time).limit(SAMPLE_MATCHES)).all()

    calls_used = 0
    results = {}
    for book in books:
        stats = {"events_queried": 0, "non_empty": 0, "empty": 0, "errors": 0,
                 "leagues": set(), "markets": set(), "ml3way": False,
                 "spread2way": False, "live_odds": False, "latencies": [], "last_success": None}
        for m in sample:
            if calls_used >= max_calls:
                break
            t0 = time.monotonic()
            try:
                odds = provider.fetch_odds(m.ext_id, source=book)
                latency_ms = (time.monotonic() - t0) * 1000
                calls_used += 1
                stats["events_queried"] += 1
                stats["latencies"].append(latency_ms)
                if odds:
                    stats["non_empty"] += 1
                    stats["leagues"].add(m.league)
                    for o in odds:
                        stats["markets"].add(o["market"])
                        if o["market"] == "ML_3WAY":
                            stats["ml3way"] = True
                        if o["market"] == "SPREAD_2WAY":
                            stats["spread2way"] = True
                        if (m.start_time - o["collected_at"]).total_seconds() <= 0:
                            stats["live_odds"] = True
                    stats["last_success"] = _now()
                else:
                    stats["empty"] += 1
            except Exception:
                stats["errors"] += 1
                calls_used += 1

        # Status semantics: any real non-empty payload proves WORKS, even if
        # other calls in the same scan errored or came back empty. Zero
        # non-empty + at least one error = BROKEN. Zero non-empty + zero
        # errors (just empty responses) = EMPTY, never BROKEN.
        if stats["non_empty"] > 0:
            status = "WORKS"
        elif stats["errors"] > 0:
            status = "BROKEN"
        elif stats["events_queried"] > 0:
            status = "EMPTY"
        else:
            status = "UNKNOWN"

        existing = db.scalar(select(BookmakerCoverage).where(BookmakerCoverage.source_name == book))
        row = existing or BookmakerCoverage(source_name=book)
        row.scanned_at = _now()
        row.events_queried = stats["events_queried"]
        row.non_empty_responses = stats["non_empty"]
        row.empty_responses = stats["empty"]
        row.error_responses = stats["errors"]
        row.leagues_seen_json = json.dumps(sorted(stats["leagues"]))
        row.markets_seen_json = json.dumps(sorted(stats["markets"]))
        row.ml_3way_available = stats["ml3way"]
        row.spread_2way_available = stats["spread2way"]
        row.live_odds_available = stats["live_odds"]
        row.response_latency_ms_avg = (round(sum(stats["latencies"]) / len(stats["latencies"]), 1)
                                       if stats["latencies"] else None)
        # A single scan (pre-match sample) cannot measure first-live latency;
        # that requires an actual live transition, which is exactly what the
        # hot poller measures. Never claim a number the scanner didn't earn.
        row.first_live_availability = "UNKNOWN"
        if stats["last_success"]:
            row.last_successful_observation = stats["last_success"]
        row.status = status
        row.execution_candidate = (status == "WORKS" and book != REFERENCE_BOOK)
        if existing is None:
            db.add(row)
        results[book] = {"status": status, "non_empty": stats["non_empty"],
                         "empty": stats["empty"], "errors": stats["errors"]}
    db.commit()
    return {"skipped": False, "calls_used": calls_used, "results": results}


def list_coverage(db: Session) -> list[dict]:
    rows = db.scalars(select(BookmakerCoverage).order_by(BookmakerCoverage.source_name)).all()
    out = []
    for r in rows:
        out.append({
            "source_name": r.source_name,
            "is_reference_feed": r.source_name == REFERENCE_BOOK,
            "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
            "events_queried": r.events_queried, "non_empty_responses": r.non_empty_responses,
            "empty_responses": r.empty_responses, "error_responses": r.error_responses,
            "leagues_seen": json.loads(r.leagues_seen_json or "[]"),
            "markets_seen": json.loads(r.markets_seen_json or "[]"),
            "ml_3way_available": r.ml_3way_available, "spread_2way_available": r.spread_2way_available,
            "live_odds_available": r.live_odds_available,
            "response_latency_ms_avg": r.response_latency_ms_avg,
            "first_live_availability": r.first_live_availability,
            "last_successful_observation": r.last_successful_observation.isoformat() if r.last_successful_observation else None,
            "status": r.status, "execution_candidate": r.execution_candidate, "notes": r.notes,
        })
    return out
