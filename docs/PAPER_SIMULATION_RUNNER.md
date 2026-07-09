# Paper Simulation Runner (v0.3.7C)

**Paper only. No live betting, no bet placement automation, anywhere in
this chain.**

```bash
python3 scripts/simulations/run_daily_paper_sim.py
```

Writes `notes/simulations/YYYY-MM-DD-paper-sim.md`,
`notes/simulations/latest_paper_sim.json`, and appends one row per run to
`notes/simulations/simulation_history.csv`.

## What it composes (not re-derives)

This script does not reimplement pricing, settlement, or CLV logic — it
calls the existing, already-tested engines:
`engines/paper_trade.py` (fills/settlement), `engines/winner_edge.py`
(winner accuracy, favorite baseline, ROI-by-delay), `engines/execution_classifier_v2.py`
(primary states/flags), `engines/entry_floor_diagnostics.py` (floor
what-ifs), `engines/clv_forward_readiness.py` (provider-time vs.
system-time CLV), `engines/market_availability.py` (prevalence).

## Sections

- **A. Historical replay** — always labeled `DEGRADED (provider-time
  historical rows)`. Eligible signals, filled trades, fill rate, realized
  paper ROI by delay bucket, average odds taken, win rate, draw exposure,
  max drawdown (units), longest losing streak, favorite-baseline margin,
  the 1/2/4/6% entry-floor what-if simulation, and the full execution-state
  distribution.
- **B. Forward clean simulation** — only `ExecutionClassification` rows
  with `is_historical_degraded=False` (i.e. built from real
  `polled_at`/`ingested_at` timestamps). Labeled `CLEAN` once any such rows
  exist; `PENDING (0 forward rows yet)` otherwise — that is the expected,
  honest state immediately after this release ships.
- **C. CLV-first** — historical (`DEGRADED`) and forward (`system-time`)
  CLV reported in **separate keys**, never merged into one number.
- **D. Entry timing (delay comparison table)** — for each of
  0/5/10/20/30/45s: fill rate, no-data rate, price-below-floor rate, sample
  size, and ROI **only** once filled-trade count reaches 300
  (`roi_descriptive_only=True` below that, and the ROI value itself is
  `null`, not a misleadingly-precise small-sample number).
- **E. Market availability** — reuses the v0.3.7B prevalence report
  verbatim (see `notes/triage/v0_3_7B-market-availability.md`).
- **F. Friend pick shadow simulation** — clean picks require
  `clean_scored=TRUE`, a known `price_at_receipt`, `book`, and
  `market_type`, and must not be `logged_after_result=TRUE`. Retro picks
  are counted separately and excluded from the clean sample. Correlated
  legs are grouped by `signal_group_id`.

## Sample-size gates

Identical table to the Daily Research Loop (50 / 150 / 400), plus one
additional rule: **filled trades below 300 make ROI descriptive-only** —
the number is computed but flagged, never presented as if it were reliable.

## Allowed final verdicts (exactly one, chosen by priority)

```
NOT ENOUGH DATA
DATA QUALITY BLOCKED
EXECUTION BLOCKED
SOURCE/FEED BLOCKED
MODEL UNDERPERFORMS BASELINE
MODEL SHOWS DIRECTIONAL CLV ONLY
MODEL SHOWS CLEAN FORWARD EDGE CANDIDATE
```

Priority order (`final_verdict()` in `run_daily_paper_sim.py`):
1. Health status `FAIL` → `SOURCE/FEED BLOCKED`.
2. Distinct sample n < 50 → `NOT ENOUGH DATA`.
3. `NO_DATA_AT_ENTRY` > 70% of all classified execution states → `EXECUTION BLOCKED`.
4. Model margin vs. favorite baseline is negative → `MODEL UNDERPERFORMS BASELINE`.
5. Forward-clean n < 150 → `MODEL SHOWS DIRECTIONAL CLV ONLY`.
6. Otherwise → `MODEL SHOWS CLEAN FORWARD EDGE CANDIDATE`.

As of this release, on real historical data, the verdict is
**`EXECUTION BLOCKED`** (NO_DATA_AT_ENTRY is 81% of classified trades) —
stated here as the honest, current answer, not a target to beat.
