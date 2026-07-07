#!/usr/bin/env python3
"""v0.3.7A Gate G1 -- Schema & timestamp census. READ-ONLY.

Lists every timestamp column on odds_snapshots, prediction_ledger, and
paper_trades, classifies what each one actually represents, and shows
sample rows as evidence. Issues SELECT-only queries against the real
esoccer.db. No writes, no schema changes.
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "backend" / "esoccer.db"


def cols(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [(r[1], r[2]) for r in cur.fetchall()]  # (name, type)


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    out = {"tables": {}}

    for table in ("odds_snapshots", "prediction_ledger", "paper_trades", "matches",
                  "raw_provider_responses", "market_events"):
        all_cols = cols(cur, table)
        ts_cols = [c for c, t in all_cols if "DATETIME" in t.upper() or "date" in c.lower()
                  or c.lower() in ("collected_at", "prediction_time", "scheduled_start",
                                   "signal_time", "created_at", "start_time", "at",
                                   "captured_at", "scored_at")]
        out["tables"][table] = {"all_columns": all_cols, "timestamp_columns": ts_cols}

    con2 = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con2.row_factory = sqlite3.Row
    cur2 = con2.cursor()

    # 10 sample odds_snapshots rows, ordered by id, spanning early + late.
    cur2.execute("""
        SELECT id, match_id, sportsbook, market, selection, collected_at, phase,
               seconds_to_kickoff, is_opening, is_closing
        FROM odds_snapshots ORDER BY id LIMIT 5
    """)
    early = [dict(r) for r in cur2.fetchall()]
    cur2.execute("""
        SELECT id, match_id, sportsbook, market, selection, collected_at, phase,
               seconds_to_kickoff, is_opening, is_closing
        FROM odds_snapshots ORDER BY id DESC LIMIT 5
    """)
    late = [dict(r) for r in cur2.fetchall()]
    out["odds_snapshots_samples"] = early + late

    # Cross-reference: raw_provider_responses.at (DB-insert time of the raw
    # HTTP response) vs the collected_at values seen in the SAME rough time
    # window, to show the two are different clocks/meanings.
    cur2.execute("""
        SELECT id, at, provider, endpoint, sportsbook, length(payload) as payload_len
        FROM raw_provider_responses WHERE endpoint='/v2/event/odds' ORDER BY id DESC LIMIT 5
    """)
    out["raw_provider_responses_samples"] = [dict(r) for r in cur2.fetchall()]

    # PredictionLedger samples
    cur2.execute("""
        SELECT id, match_id, horizon_label, prediction_time, scheduled_start, created_at, status
        FROM prediction_ledger ORDER BY id LIMIT 5
    """)
    pl_early = [dict(r) for r in cur2.fetchall()]
    cur2.execute("""
        SELECT id, match_id, horizon_label, prediction_time, scheduled_start, created_at, status
        FROM prediction_ledger ORDER BY id DESC LIMIT 5
    """)
    pl_late = [dict(r) for r in cur2.fetchall()]
    out["prediction_ledger_samples"] = pl_early + pl_late

    # PaperTrade samples
    cur2.execute("""
        SELECT id, signal_source, signal_id, signal_time, delay_seconds, created_at, settlement_status
        FROM paper_trades ORDER BY id LIMIT 5
    """)
    pt_early = [dict(r) for r in cur2.fetchall()]
    cur2.execute("""
        SELECT id, signal_source, signal_id, signal_time, delay_seconds, created_at, settlement_status
        FROM paper_trades ORDER BY id DESC LIMIT 5
    """)
    pt_late = [dict(r) for r in cur2.fetchall()]
    out["paper_trades_samples"] = pt_early + pt_late

    # Row counts for context
    for t in ("odds_snapshots", "prediction_ledger", "paper_trades", "matches", "raw_provider_responses"):
        cur2.execute(f"SELECT COUNT(*) FROM {t}")
        out["tables"][t]["row_count"] = cur2.fetchone()[0]

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
