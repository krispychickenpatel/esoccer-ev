# v0.3.6.2 — Profit Core Repair: Paper Trades + Winner Edge Truth Layer

Focused profit-core repair. Not a feature build, not v0.3.7. No advanced ML,
no betting automation, no bankroll/risk rule changes, no bookmaker scans
run, no fake/demo data, nothing committed automatically. Zero schema
changes — every fix is logic-level only.

## Blunt summary

Paper trades existed but were disconnected from the model: `simulate_all`
and `simulate_model_candidate` gated eligibility on
`PredictionLedger.action == "BET"`. In the real dataset, **0 of 653**
predictions have `action="BET"` (322 PASS, 331 WAIT) — the model's combined
EV+steam-probability bar has never fired — so this gate silently zeroed out
all model paper trading regardless of how much real scored data existed.
Meanwhile `signal_gate_model` PASSed at 82% "steam accuracy," creating a
false impression of health. Fixing the gate and adding the Winner Edge
Truth Layer immediately surfaced the truth: **the model underperforms the
naive favorite baseline by 9.5 points and shows negative paper ROI at every
delay bucket (0-45s)**. Steam-direction accuracy and winning/profit are not
the same thing, and this release proves it with real data instead of
prose.

## Part A — Fixed model paper trades

- **Root cause**: `action == "BET"` gate in
  `paper_trade.simulate_model_candidate()` / `routers/paper_trades.py`'s
  `simulate_all`. Removed entirely. Eligibility is now based on structural
  data sufficiency (`match_id`, `selection`, `prediction_time` present) —
  NOT on the model's own action decision, and NOT on `execution_mode`
  (NULL for all 653 legacy predictions, since that field postdates them —
  explicitly not used as a gate either, so this doesn't just trade one bad
  filter for another).
- `_model_max_entry()`: fallback order for the entry-price ceiling —
  (1) `maximum_entry_decimal` if present, (2) `current_decimal` (always
  present on every row), (3) `None` (no ceiling; `simulate_signal` already
  treats that as "any found price survives" — never fabricates a price
  either way).
- New `GET /api/paper-trades/eligibility` — reports `prediction_ledger_total`,
  `scored_predictions`, `eligible_signals`, `already_simulated_signals`,
  `skipped_signals`, `skip_reasons` (per real field-level cause), plus
  `expected_delay_rows`/`existing_delay_rows`/`missing_delay_rows` and
  informational (non-blocking) `legacy_execution_mode_null_count` /
  `pending_result_count`. Same shape for `friend`.
- `POST /api/paper-trades/simulate-all` rewritten (logic moved into
  `engines/paper_trade.py::simulate_all()`): richer per-source stats
  (`eligible_signals`, `created_trades`, `existing_trades`,
  `skipped_signals`, `skip_reasons`) plus the original top-level fields kept
  for backward compatibility. Idempotent — verified live: first run created
  3918 model trades (653 predictions × 6 delays), second run created 0 and
  reported all 3918 as `existing_trades`.
- `GET /api/paper-trades/report` now separates `MODEL` and `FRIEND` under
  `by_source`, each with its own per-delay fill rate / missed-price count /
  settled count / total paper P/L / avg proxy CLV. The old combined
  `by_delay_seconds` is kept for backward compatibility. Impossible to
  mistake 6 friend rows for model execution evidence anymore — verified
  live (3918 model trades vs. 6 friend trades, clearly separated).

## Part B — Winner Edge Truth Layer

New `engines/winner_edge.py`, `GET /api/profit/winner-edge`. Directly
answers the actual thesis question instead of proxying through steam:

- **Model**: total/scored predictions, `distinct_samples` (deduplicated by
  `(match_id, selection)`, latest `prediction_time` wins ties — same rule
  as `profit_gates.signal_gate`), winner accuracy, favorite-baseline
  accuracy, margin, favorite/underdog split, odds-bucket accuracy
  (`<1.50`/`1.50-1.80`/`1.80-2.20`/`2.20-3.00`/`>3.00`), model edge
  (`model_prob - devigged_or_market_implied_prob`), Brier score,
  calibration buckets (`0-40%`...`70%+`), and — pulling directly from real
  `PaperTrade` rows, never recomputed — paper P/L/fill-rate/missed-rate/CLV
  by delay bucket and ROI% by delay bucket.
- **Friend**: total/clean-pre-kick/backfilled/likely-test-artifact/scored
  counts, winner accuracy, favorite baseline, odds-bucket accuracy, the same
  paper-trade-derived execution/ROI metrics, and an explicit
  `book_proxy_caveat`.
- **Leakage rules enforced and tested**: sample selection uses only
  `prediction_time` (known at freeze time) to break horizon ties — never
  `match.winner` or reality data; de-vig and market-implied probability are
  computed from snapshots at-or-before the prediction's own
  `prediction_time`, confirmed not to see a later live-phase price swing;
  `winner_correct`/`actual_winner` are only populated when
  `status == "scored"` AND the match actually has a result.
- **Deliberate, documented dedup split**: winner-accuracy/baseline/Brier
  metrics dedupe to one sample per (match, selection); paper-trade-derived
  execution metrics (fill rate, ROI, CLV by delay) are computed over ALL
  simulated paper trades, since each horizon is a genuinely distinct
  hypothetical entry time, not a repeated observation of the same fact —
  this is intentionally different from the winner-accuracy dedup rule, not
  an inconsistency.

**Live result** (real data, this session): model winner accuracy **43.7%**
vs. favorite baseline **53.2%** (margin **-9.5 pts** — the model loses to
just betting the favorite), Brier score **0.2627** (worse than a trivial
constant-0.5 forecast), ROI by delay **-9.02% to -13.67%, negative at every
single bucket**.

## Part C — Profit gates adjustments

- `execution_gate` / `risk_gate` now expose `model_n` / `friend_n`
  alongside the existing combined `n`.
- New `winner_edge_gate_model` / `winner_edge_gate_friend`: PASS requires
  enough independent samples (model n≥50, friend n≥30 *clean* — excludes
  backfilled and likely-test-artifact picks specifically for this gate),
  winner accuracy beating the favorite baseline, AND at least one delay
  bucket with non-negative paper ROI. Missing paper-trade data forces NOT
  ENOUGH DATA, never a default PASS.
- Both new gates now feed into `compute_all_gates`'s overall status list.

**Live gate result** (real data, this session): `execution_gate` **FAIL**
(13.5% survival vs. 60% required, n=654), `risk_gate` **FAIL** (max
drawdown 89.69 units vs. 15 allowed), `winner_edge_gate_model` **FAIL**
(margin -9.5pts). `signal_gate_model` still PASSes at 82.2% steam accuracy
— now clearly shown to be irrelevant to actual profitability once the other
gates are visible side by side. `ready_for_live_small_stakes` remains **NOT
ENOUGH DATA** overall (friend-side gates still lack sample), but with
friend data this would flip to **FAIL**, not closer to PASS.

## Part D — Tests

New `backend/tests/test_profit_core_repair_v0362.py` (12 tests, all
passing): legacy `execution_mode=NULL` prediction is paper-trade eligible;
`simulate_all` creates MODEL trades from eligible historical predictions;
running `simulate_all` twice never duplicates rows; missing price always
produces `MISSED_PRICE`, never a fabricated fill; `/eligibility` reports
real skip reasons; `/report` separates MODEL and FRIEND; gates expose
`model_n`/`friend_n`; winner-edge returns model winner accuracy and
favorite baseline; winner-edge dedups by `(match_id, selection)` (6
horizons of the same match/selection collapse to 1 sample, not 6); winner
edge does not use post-kickoff/live prices or future results to select or
price a sample; ROI-by-delay is computed from `PaperTrade` rows only; seed
data stays off.

**82/82 backend tests pass** (70 pre-existing + 12 new). `npm run build`
clean, no TypeScript errors. Frontend: Profit Readiness page now shows
`model_n`/`friend_n` on execution/risk gates, two new winner-edge gate
cards, and a full Winner Edge Truth Layer section (model/friend winner
accuracy, favorite baseline, ROI-by-delay table, fill/missed-rate-by-delay
table, sample warnings).

## Data preservation

Verified before/after: `matches` (281), `odds_snapshots` (4403),
`prediction_ledger` (653), `prediction_reality` (398), `friend_picks` (1)
row counts unchanged. `GET /api/lab/verify-integrity`: 653/653, 0
mismatched. `GET /api/friend-picks/verify-integrity`: 1/1 valid. No schema
changes were made or needed.

## Not done / explicitly out of scope

No advanced ML. No betting automation. No bankroll/risk rule changed. No
bookmaker coverage scan run. Not moving to v0.3.7 — this is a repair pass,
and its own findings (negative ROI, gate failures) argue strongly against
treating the platform as anywhere near live-money-ready.
