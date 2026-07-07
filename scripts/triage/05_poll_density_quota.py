#!/usr/bin/env python3
"""v0.3.7A -- Poll density & quota census. READ-ONLY, SELECT-only.

Odds rows per match in the final 10 minutes before actual match start
(same actual-start proxy as script 04: earliest phase='live' collected_at,
else Match.start_time). Also reports raw_provider_responses call volume
over time as the only historical proxy for API usage -- BetsAPI's own
quota_remaining/quota_limit are live HTTP response headers, tracked
in-memory per provider instance, and are NOT persisted anywhere, so
historical quota consumption cannot be reconstructed exactly from stored
data. Reported honestly as a limitation, not papered over.
"""
import json
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "backend" / "esoccer.db"


def parse(ts):
    if ts is None:
        return None
    ts = ts.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT id, start_time FROM matches")
    matches = [dict(r) for r in cur.fetchall()]

    counts = []
    for m in matches:
        cur.execute("""
            SELECT collected_at, phase FROM odds_snapshots WHERE match_id = ?
            ORDER BY collected_at
        """, (m["id"],))
        odds = [dict(r) for r in cur.fetchall()]
        live_rows = [o for o in odds if o["phase"] == "live"]
        actual_start = parse(min(o["collected_at"] for o in live_rows)) if live_rows else parse(m["start_time"])
        if actual_start is None:
            continue
        cutoff = actual_start - timedelta(minutes=10)
        window_rows = [o for o in odds if cutoff <= parse(o["collected_at"]) <= actual_start]
        if window_rows or live_rows:
            counts.append(len(window_rows))

    dist = {}
    if counts:
        dist = {
            "matches_with_any_final10min_window": len(counts),
            "min": min(counts), "max": max(counts),
            "mean": round(statistics.mean(counts), 2),
            "median": statistics.median(counts),
            "p10": sorted(counts)[int(0.10 * (len(counts) - 1))],
            "p90": sorted(counts)[int(0.90 * (len(counts) - 1))],
            "zero_rows_count": sum(1 for c in counts if c == 0),
            "one_row_count": sum(1 for c in counts if c == 1),
            "lt2_rows_count": sum(1 for c in counts if c < 2),
        }

    # raw_provider_responses call volume as the only historical proxy for
    # actual request rate (NOT the same as BetsAPI's quota window, which is
    # not stored).
    cur.execute("SELECT MIN(at), MAX(at), COUNT(*) FROM raw_provider_responses")
    lo, hi, total = cur.fetchone()
    span_hours = None
    calls_per_hour = None
    if lo and hi:
        lo_dt, hi_dt = parse(lo), parse(hi)
        span_hours = (hi_dt - lo_dt).total_seconds() / 3600 if hi_dt and lo_dt else None
        if span_hours and span_hours > 0:
            calls_per_hour = round(total / span_hours, 1)

    cur.execute("SELECT endpoint, COUNT(*) c FROM raw_provider_responses GROUP BY endpoint ORDER BY c DESC")
    by_endpoint = [dict(r) for r in cur.fetchall()]

    out = {
        "final_10min_odds_rows_per_match_distribution": dist,
        "raw_provider_responses_total": total,
        "raw_provider_responses_span_start": lo,
        "raw_provider_responses_span_end": hi,
        "raw_provider_responses_span_hours": round(span_hours, 2) if span_hours else None,
        "observed_calls_per_hour_over_full_span": calls_per_hour,
        "raw_provider_responses_by_endpoint": by_endpoint,
        "documented_plan_quota_note": ("3,600 req/hr cap documented in docs/DECISIONS.md as the "
                                       "$30/mo BetsAPI plan limit (narrative, not a stored/enforced "
                                       "config constant). Live quota_remaining/quota_limit come from "
                                       "X-RateLimit-* response headers, tracked in-memory per "
                                       "BetsApiProvider instance only -- NOT persisted to DB or logs, "
                                       "so historical quota consumption cannot be reconstructed "
                                       "exactly, only approximated via raw_provider_responses call counts."),
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
