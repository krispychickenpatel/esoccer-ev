# v0.3.4 — Stabilization Pass

Scope: bug fixes, leakage guards, integrity checks. No new strategy, no new ML,
no schema changes. Existing SQLite databases keep working unchanged.

## Fixes

### 1. Steam Predictor leakage (violation of rule 3, "no future information leakage")
**File:** `backend/app/engines/steam.py`
`steam_prediction_for_snapshot` bounded its history at `min(kickoff, now())`.
Real-time freezes were safe, but any freeze with a historical `prediction_time`
(Prediction Lab backfill, `allow_late`, replays, tests) could consume
pre-live → first-live pairs that only settled **after** the prediction moment.
Fix: new `as_of` parameter; history is bounded by `min(kickoff, as_of)`.
The Lab always passes `as_of=prediction_time`. Real-time callers (Pick Engine,
Best Picks) are unchanged (`as_of=None` → now).

### 2. Movement-signal target leakage
**Files:** `backend/app/engines/movement.py`, `signals.py`, `prediction_lab.py`
`movement_signal_for` had no time cutoff. Freezing a prediction for a match
that already had live snapshots let the ensemble ingest that match's own
first-live jump — the exact quantity the steam model is later scored against.
Fix: `as_of` cutoff threaded through `match_timeline` → `movement_metrics` →
`movement_signal_for` → `all_signals`; the Lab passes `prediction_time`.

### 3. League-signal lookahead
**File:** `backend/app/engines/signals.py`
League draw-rate context counted every finished match in the league, including
ones that kicked off after the match being predicted. Now filtered to
`start_time < m.start_time`.

### 4. Live-phase poll cadence silently capped at 10s
**File:** `backend/app/services/poller.py`
The loop slept a flat 10s, so the documented 2s live bucket (D17) never
actually ran at 2s. First-live granularity is the core thesis measurement.
Fix: sleep derived from the tightest cadence among tracked matches,
clamped to [1s, 30s].

### 5. Lab cycle could starve odds capture
**File:** `backend/app/services/poller.py`
`run_prediction_lab_cycle` ran on every loop tick; reality capture scans every
match with odds and grows with the DB. Throttled to once/20s (well inside the
75s horizon tolerance) so lab bookkeeping never delays a first-live snapshot.

### 6. `allow_late` horizon-label contamination
**File:** `backend/app/engines/prediction_lab.py`
`freeze_due_predictions(allow_late=True)` froze **every** passed horizon with
the current timestamp — a "T-30m" row could be created 3 minutes before
kickoff, corrupting per-horizon model comparison. Late freezes now produce only
the single label nearest the real seconds-to-kickoff.

### 7. Frozen-ledger integrity is now verifiable
**Files:** `backend/app/engines/prediction_lab.py`, `routers/lab.py`,
`frontend/src/pages/PredictionLab.tsx`
`immutable_hash` was written but never checked. Added
`verify_integrity()` + `GET /api/lab/verify-integrity`, which recomputes each
frozen row's sha256 from its stored fields and compares. The dashboard includes
a `ledger_integrity` block and the UI shows a red tamper warning on mismatch.

### 8. UI timestamps shifted by viewer's UTC offset
**File:** `frontend/src/api.ts`
Backend emits naive UTC ISO strings; `new Date("...T12:00:00")` parses as
local time in JS. All displayed times (prediction times, kickoff, captures)
were off by the local UTC offset. `fmtDT` now appends `Z` to offset-less
strings.

### 9. None-safety in scoring
`steam_probability` comparisons in `_score_error_bucket` and
`score_predictions` are now None-safe (`or 0.5`).

### 10. Minor
- Removed dead variable in `dashboard()`.
- Dashboard totals now include `pending_scores`.
- Version bumped everywhere: API `0.3.4-prediction-lab`, `LAB_VERSION`
  `prediction_lab_v0.3.4`, frontend package + sidebar badge.
  `MODEL_VERSION` (`self_test_pick_engine_v1`) intentionally unchanged so
  existing scored groups keep comparing.

## Tests
4 new regression tests in `backend/tests/test_prediction_lab.py`:
- `test_late_freeze_cannot_see_own_live_ticks` — movement + steam leakage guard
- `test_steam_history_bounded_by_as_of` — history cutoff honors `as_of`
- `test_ledger_integrity_detects_tampering` — sha256 verify catches edits
- `test_allow_late_freezes_only_nearest_horizon` — honest horizon labels

27/27 backend tests pass. Frontend `tsc -b && vite build` passes.

## Known remaining risks (not fixed on purpose)
1. **Pre/live classification uses scheduled start time**, not provider in-play
   state. A match that starts late gets its final pre-kickoff ticks
   misclassified as live (consistent across capture + steam, so measurement is
   internally coherent, but "first-live" is really "first tick after scheduled
   KO"). Fix belongs with BetsAPI in-play state validation.
2. **BetsAPI first-live latency is still unproven** — capability report still
   shows UNKNOWN until probed with a real key. Do not trust steam metrics
   until `/api/provider/capability-report` shows odds + in-play WORKS.
3. **Closing odds = last snapshot seen**, which under the current cadence table
   (~2min post-KO pull) is a proxy, not a true closing line.
4. **N+1 query patterns** in `model_comparison`/`ledger_rows` are fine at
   research scale (thousands of rows) but will need batching if the ledger
   grows past ~50k rows.
5. **`shadow_signal` has no time cutoff** — a Recommendation entered after
   prediction_time would leak into a backfilled freeze. Low priority: shadow
   recs are manual and rare; flagged for the next pass.
