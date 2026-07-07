#!/usr/bin/env python3
"""v0.3.7A Gate G4 -- Close-feasibility waterfall. READ-ONLY, SELECT-only.

Unit of analysis: distinct (match_id, selection) pairs from scored
PredictionLedger rows, market=ML_3WAY, sportsbook=bet365 (the reference
feed) -- consistent with the (match_id, selection) dedup convention already
used by profit_gates.signal_gate / winner_edge.

Filters, applied in order, each computed over the survivors of the previous
step:
  0. universe: distinct (match_id, selection) with a scored prediction and
     a known match result.
  1. actual-start proxy exists AND a last odds row exists at/before it.
     actual_start_proxy = earliest phase='live' collected_at for that match
     if one exists (a genuine live-start signal), else Match.start_time
     (scheduled kickoff) as a flagged fallback.
  2. that last pre-actual-start odds row's collected_at is within 60s of
     actual_start_proxy (freshness).
  3. all three ML_3WAY outcomes (home/draw/away) were priced in that same
     near-start window (+/- 5s of the chosen row's collected_at).
  4. not suspended: no MarketEvent(event_type='disappeared') for that
     (match, sportsbook, market, selection) within 5 min before
     actual_start_proxy. (No explicit "suspended" status exists in this
     schema -- this is the closest stored proxy, and is reported as such.)
  5. >=2 odds updates for that (match, sportsbook, market, selection) in
     the final 5 minutes before actual_start_proxy.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "backend" / "esoccer.db"
SPORTSBOOK = "bet365"
MARKET = "ML_3WAY"


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

    # Step 0: universe -- distinct (match_id, selection), latest prediction_time wins.
    cur.execute("""
        SELECT pl.id, pl.match_id, pl.selection, pl.prediction_time, m.start_time, m.winner
        FROM prediction_ledger pl
        JOIN matches m ON m.id = pl.match_id
        WHERE pl.status = 'scored' AND pl.market = ? AND pl.sportsbook = ?
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    """, (MARKET, SPORTSBOOK))
    rows = [dict(r) for r in cur.fetchall()]
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["match_id"], r["selection"])
        if key not in best or r["prediction_time"] > best[key]["prediction_time"]:
            best[key] = r
    universe = list(best.values())
    n0 = len(universe)

    # Preload all odds rows per match for this sportsbook/market, sorted.
    match_ids = sorted({u["match_id"] for u in universe})
    odds_by_match: dict[int, list[dict]] = {}
    for mid in match_ids:
        cur.execute("""
            SELECT selection, collected_at, phase FROM odds_snapshots
            WHERE match_id = ? AND sportsbook = ? AND market = ?
            ORDER BY collected_at
        """, (mid, SPORTSBOOK, MARKET))
        odds_by_match[mid] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT DISTINCT match_id, selection FROM market_events WHERE event_type='disappeared'")
    disappeared_by_match: dict[int, set] = {}
    for r in cur.fetchall():
        disappeared_by_match.setdefault(r["match_id"], set()).add(r["selection"])
    cur.execute("SELECT match_id, selection, at FROM market_events WHERE event_type='disappeared'")
    disappeared_events = [dict(r) for r in cur.fetchall()]

    step1, step2, step3, step4, step5 = [], [], [], [], []
    fallback_flagged = 0

    for u in universe:
        mid = u["match_id"]
        odds = odds_by_match.get(mid, [])
        live_rows = [o for o in odds if o["phase"] == "live"]
        if live_rows:
            actual_start = parse(min(o["collected_at"] for o in live_rows))
            used_fallback = False
        else:
            actual_start = parse(u["start_time"])
            used_fallback = True

        if actual_start is None:
            continue

        pre_rows = [o for o in odds if o["selection"] == u["selection"] and parse(o["collected_at"]) is not None
                   and parse(o["collected_at"]) <= actual_start]
        if not pre_rows:
            continue
        last_pre = max(pre_rows, key=lambda o: parse(o["collected_at"]))
        step1.append({**u, "actual_start": actual_start, "used_fallback": used_fallback,
                     "last_pre_collected_at": last_pre["collected_at"]})
        if used_fallback:
            fallback_flagged += 1

    for u in step1:
        gap_s = (u["actual_start"] - parse(u["last_pre_collected_at"])).total_seconds()
        if gap_s <= 60:
            step2.append({**u, "freshness_gap_s": gap_s})

    for u in step2:
        window_lo = parse(u["last_pre_collected_at"]) - timedelta(seconds=5)
        window_hi = parse(u["last_pre_collected_at"]) + timedelta(seconds=5)
        odds = odds_by_match.get(u["match_id"], [])
        selections_priced = {o["selection"] for o in odds
                             if window_lo <= parse(o["collected_at"]) <= window_hi}
        if {"home", "draw", "away"} <= selections_priced:
            step3.append(u)

    for u in step3:
        cutoff = u["actual_start"] - timedelta(minutes=5)
        suspended = any(
            e["match_id"] == u["match_id"] and e["selection"] == u["selection"]
            and parse(e["at"]) is not None and cutoff <= parse(e["at"]) <= u["actual_start"]
            for e in disappeared_events
        )
        if not suspended:
            step4.append(u)

    for u in step4:
        cutoff = u["actual_start"] - timedelta(minutes=5)
        odds = odds_by_match.get(u["match_id"], [])
        updates = [o for o in odds if o["selection"] == u["selection"]
                  and cutoff <= parse(o["collected_at"]) <= u["actual_start"]]
        if len(updates) >= 2:
            step5.append(u)

    out = {
        "sportsbook": SPORTSBOOK, "market": MARKET,
        "step0_universe_scored_distinct_match_selection": n0,
        "step1_last_odds_row_ateq_actual_start_exists": len(step1),
        "step1_used_scheduled_kickoff_fallback_flagged": fallback_flagged,
        "step2_availability_age_lte_60s": len(step2),
        "step3_all_three_outcomes_priced_near_close": len(step3),
        "step4_not_suspended_proxy": len(step4),
        "step5_ge2_updates_final_5min": len(step5),
        "final_decision_grade_n": len(step5),
        "gate_g4_directional_threshold": 50,
        "gate_g4_decision_grade_threshold": 150,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
