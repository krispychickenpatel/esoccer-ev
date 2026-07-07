#!/usr/bin/env python3
"""v0.3.7A Gate G3 -- maximum_entry_decimal invariant query. READ-ONLY.

For MODEL paper trades, joins back to the originating PredictionLedger row
to get the signal-time price and the predicted movement direction, then
computes delta = entry-window price - signal-time price, split by:
  - FILLED (price found, price >= max_entry_decimal)
  - MISSED_PRICE_BELOW_FLOOR (price found, price < max_entry_decimal)
  - MISSED_PRICE_NO_DATA (no snapshot found at all -- price_decimal IS NULL,
    delta cannot be computed, reported as a separate count only)

predicted_direction: -1 if steam predicted shortening
(predicted_first_live_decimal < current_decimal, or steam_probability>=0.58),
else +1 (drift/no-shorten expected). "Aligned" = sign(delta) matches
predicted_direction (delta<0 aligned with -1, delta>=0 aligned with +1).
"""
import json
import sqlite3
import statistics
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "backend" / "esoccer.db"


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT pt.id as trade_id, pt.delay_seconds, pt.price_decimal, pt.max_entry_decimal,
               pt.settlement_status,
               pl.current_decimal as signal_price, pl.predicted_first_live_decimal,
               pl.steam_probability
        FROM paper_trades pt
        JOIN prediction_ledger pl ON pl.id = pt.signal_id
        WHERE pt.signal_source = 'MODEL'
    """)
    rows = [dict(r) for r in cur.fetchall()]

    no_data = 0
    buckets = {"FILLED": [], "MISSED_PRICE_BELOW_FLOOR": []}
    aligned_counts = {"FILLED": {"aligned": 0, "not_aligned": 0},
                      "MISSED_PRICE_BELOW_FLOOR": {"aligned": 0, "not_aligned": 0}}

    for r in rows:
        if r["price_decimal"] is None:
            no_data += 1
            continue
        delta = r["price_decimal"] - r["signal_price"]
        predicted_shorten = (
            (r["predicted_first_live_decimal"] is not None
             and r["predicted_first_live_decimal"] < r["signal_price"])
            or (r["steam_probability"] or 0.5) >= 0.58
        )
        direction = -1 if predicted_shorten else 1
        survived = r["max_entry_decimal"] is None or r["price_decimal"] >= r["max_entry_decimal"]
        label = "FILLED" if survived else "MISSED_PRICE_BELOW_FLOOR"
        buckets[label].append(delta)
        aligned = (delta < 0 and direction == -1) or (delta >= 0 and direction == 1)
        aligned_counts[label]["aligned" if aligned else "not_aligned"] += 1

    def summarize(vals):
        if not vals:
            return None
        return {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 4),
            "median": round(statistics.median(vals), 4),
            "stdev": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "pct_negative_delta": round(100 * sum(1 for v in vals if v < 0) / len(vals), 1),
            "pct_positive_or_zero_delta": round(100 * sum(1 for v in vals if v >= 0) / len(vals), 1),
        }

    out = {
        "total_model_paper_trade_rows_checked": len(rows),
        "missed_price_no_data_count": no_data,
        "filled_count": len(buckets["FILLED"]),
        "missed_price_below_floor_count": len(buckets["MISSED_PRICE_BELOW_FLOOR"]),
        "delta_distribution": {
            "FILLED": summarize(buckets["FILLED"]),
            "MISSED_PRICE_BELOW_FLOOR": summarize(buckets["MISSED_PRICE_BELOW_FLOOR"]),
        },
        "direction_alignment": aligned_counts,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
