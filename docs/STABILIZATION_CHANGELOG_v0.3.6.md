# v0.3.6 — Profit Validation Layer

Follow-up to v0.3.5's Provider Execution Fix. Two clean validation sessions
(n=30, both `notes/v0.3.5-provider-execution-fix-report.md` and
`notes/fixed-max2-first-live-validation-report.md`) established as fact:
BetsAPI/bet365 ML_3WAY first-live capture has an observed **~20-30s
publication lag** (avg 26-29s, median 20-25s, p95 64-84s, 0-5% within 15s),
and it's provider-side, not poller-side — cutting tracked load 5→2 barely
moved the typical case. This release stops chasing sub-15s poller latency
and instead builds the machinery to answer the actual question: **can this
platform make money given that lag?**

No betting thesis change. No ML. No fake/demo data. Seed data stays off.
No automated live betting, no real-money recommendations, no bankroll-rule
changes. All schema additions are additive (zero-migration).

## Thesis (unchanged)

The valuable signal exists before kickoff. The platform predicts side +
expected market movement before kickoff. Execution may happen pre-kickoff,
at live-open manually (accepting the feed lag), or via a slower book if
coverage is proven. Profitability is measured after realistic execution
delay, book availability, and price movement — not against an optimistic
sub-15s reaction time that was never real.

## Module 1-2 — Friend Pick Ledger + Scoring

- New `friend_picks` / `friend_pick_scores` tables (`models.py`), new
  `engines/friend_picks.py`, new router `routers/friend_picks.py`.
- **Leakage anchor**: `effective_known_at = max(pick_timestamp, created_at)`.
  A pick entered after the fact can never be scored, priced, or compared as
  if it were known earlier than the moment it actually entered the system.
  `is_backfilled` flags any pick entered >120s after its claimed
  `pick_timestamp`.
- **Immutable**: same freeze/sha256-hash pattern as `PredictionLedger`.
  Corrections are new rows via `corrects_pick_id`; the original is never
  edited.
- **Entity resolution**: canonical-name + kickoff (±10min) + league match
  against `matches`. Exactly one candidate → RESOLVED; zero or multiple →
  PENDING (visible in the report, never silently dropped). Auto-resolution
  re-runs every 30s inside the poller's existing throttled cycle — DB-only,
  no API calls, never competes with odds polling. Manual override via
  `POST /friend-picks/{id}/resolve`.
- **Scoring** (`score_friend_pick`): winner correctness, steam-direction
  correctness (reusing `PredictionReality`, not duplicating reality
  capture), proxy-CLV (labeled "proxy" everywhere — our closing snapshot is
  not a true market close), entry-price survival, paper P/L at a
  configurable flat stake (`Settings.paper_stake_usd`, default $100/unit).
  Comparisons: **vs model** (nearest frozen `PredictionLedger` row at or
  before `effective_known_at`, else `NOT_AVAILABLE` — never freezes one
  retroactively) and **vs market-only baseline** (pre-kickoff favorite;
  baseline claims the favorite shortens). Error buckets: exactly the 7
  specified strings.
- Endpoints: `POST/GET /api/friend-picks`, `GET /api/friend-picks/report`
  (also triggers scoring of anything newly resolvable), `GET
  /api/friend-picks/{id}`, `POST /api/friend-picks/{id}/resolve`, `POST
  /api/friend-picks/{id}/correct`.
- Frontend: new **Friend Picks** page — fast entry form, pending/scored
  tables, corrections linked to originals. Steam accuracy/CLV/paper P/L
  render only for scored picks.

## Module 6 — Paper Trade Engine (no real betting, ever)

- New `paper_trades` table, `engines/paper_trade.py`, shared
  `engines/execution_pricing.py::price_at_delay()` used by both this and
  friend-pick scoring so the two never disagree about a fill.
- Simulates entry at **0/5/10/20/30/45s** after a signal's known time.
  **Never fabricates a price**: if no snapshot exists within 60s of the
  target time, the trade is `MISSED_PRICE`, not an interpolated fill.
- Every trade carries `feed_lag_caveat=true` and the report ships the exact
  required disclaimer: *"Prices come from a reference feed with an observed
  ~20-30s publication lag. Simulated fills are optimistic. This is decay
  analysis, not execution proof."*
- Endpoints: `GET /api/paper-trades`, `GET /api/paper-trades/report`
  (fill rate + paper P/L + proxy-CLV by delay bucket), `POST
  /api/paper-trades/simulate` (one signal), `POST
  /api/paper-trades/simulate-all` (bulk convenience for every BET model
  prediction and RESOLVED friend pick not yet simulated).

## Module 3 — Book Coverage Scanner

- New `bookmaker_coverage` table, `engines/book_coverage.py`. Completely
  separate from the hot poller — **refuses to run** while any tracked match
  is inside its live window (KO±2min) and is hard-capped at 40 real API
  calls per scan.
- `bet365` is the permanent reference feed and is **never** flagged
  `execution_candidate`, regardless of its own scan results. Other books
  only become candidates after a scan proves a real non-empty esoccer odds
  response. Status semantics: a non-empty response proves `WORKS`; zero
  non-empty + any error = `BROKEN`; zero non-empty + zero errors = `EMPTY`
  (never `BROKEN` — regression-tested).
- Live-verified: one real scan found bet365 `WORKS` (5/5 non-empty) and
  FanDuel `EMPTY` (5/5 empty, 0 errors) — matching the v0.3.4 finding
  exactly, now with a repeatable, separate mechanism instead of an ad-hoc
  check.
- Endpoints: `GET /api/provider/bookmaker-coverage`, `POST
  /api/provider/bookmaker-coverage/scan`. Frontend: new Book Coverage
  section on the Data Health page.
- `data_health()` now also warns when any tracked sportsbook has a ≥90%
  empty-response rate over 5+ recent calls (reused from v0.3.5's
  `sportsbook_empty_stats`, now also fed by scanner activity).

## Module 4 — Feed Shootout Prep (schema + endpoints only)

- New `feed_candidates` table, `engines/feed_comparison.py`. No paid
  integrations wired. Seeded with exactly one real-evidence row:
  BetsAPI/bet365, status `WORKS`, first-live latency note stating the
  ~20-30s observed floor with a pointer to both validation reports.
- Endpoints: `GET /api/provider/feed-comparison`, `POST
  /api/provider/feed-comparison/manual-note`.

## Module 5 — Execution Strategy Shift

- New `engines/execution_strategy.py::classify_execution_mode()` — the
  exact 4-mode decision table (`PRE_KICKOFF` / `LIVE_OPEN_MANUAL` /
  `SLOWER_BOOK` / `PASS`), using a **30-45s stress assumption**, never
  ≤15s. `SLOWER_BOOK` requires a verified `BookmakerCoverage` execution
  candidate for the market — otherwise unavailable.
- Wired into `PredictionLedger` creation (`freeze_match_horizon`) and
  `FriendPick` creation/resolution: every signal now stores
  `execution_mode` + `execution_reason_codes_json`. Both are new, nullable,
  additive columns — **not** part of `immutable_hash`, so freeze/verify
  behavior is byte-for-byte unchanged (confirmed: 653/653 ledger rows still
  verify clean after this change).
- Audited the codebase for lingering sub-15s *assumptions*: none found.
  The only remaining `<=15` references are either (a) this release's own
  code correctly documenting the real 20-30s floor, or (b) the existing
  reality-capture *quality-classification* threshold in
  `capture_reality_for_match` (protected from regression — it's a
  measurement bucket, not an assumption that 15s is achievable).

## Module 7-8 — Profit Readiness Dashboard + Profit Kill Gates

- New `engines/profit_gates.py`, router `routers/profit.py`,
  `GET /api/profit/gates`. New **Profit Readiness** frontend page.
- Every gate defaults to `NOT ENOUGH DATA` below its minimum sample; never
  silently softened.
  - **Feed gate**: `pre_kickoff` (≥80% of matches with a pre-kick snapshot
    ≤60s before KO, n≥10) and `live_open_manual` (median AND p95 first-live
    latency ≤45s, n≥30 — both sub-statuses shown, not just the pass/fail).
  - **Signal gate** (model and friend, separately): steam-direction
    accuracy ≥ baseline+5pts, n≥30 gold/silver-tier scored rows.
  - **Execution gate**: ≥60% of simulated trades still have max-entry
    price alive at the 30s delay, n≥30.
  - **Book gate**: ≥1 non-reference book verified `WORKS` for a tracked
    league + `ML_3WAY`.
  - **Risk gate**: paper max drawdown ≤15 units, n≥30 settled trades.
- `ready_for_live_small_stakes` is the AND of every gate: `NOT ENOUGH DATA`
  if any gate lacks sample, else `FAIL` unless every single gate passes.
- **Live-verified against the real database**: `feed_gate.live_open_manual`
  correctly shows median 25.0s (PASS sub-status) / p95 60.0s (FAIL
  sub-status) → overall FAIL, n=278 — an honest, real reflection of the
  20-30s floor, not a guess.

## Module 9 — Tests

New `backend/tests/test_profit_validation.py` (13 tests, all passing):
friend-pick immutability + tamper detection; corrections create new rows,
never edit originals; backfilled `effective_known_at` never precedes
`created_at` and can't leak into earlier scoring/pricing; end-to-end friend
scoring after odds+result exist; baseline uses the favorite selection;
book coverage marks an empty-but-reachable book `EMPTY` not `BROKEN`;
scanner refuses during the live window; paper trade never fabricates a
fill on missing/stale price; paper trade delay-bucket fill-rate math;
profit gates default to `NOT ENOUGH DATA` on an empty DB; feed gate does
**not** pass a ≤15s live-reaction assumption under a realistic recorded
latency distribution; seed data and the bet365-only default stay correct.

All 48 backend tests pass (35 pre-existing + 13 new). `npm run build`
succeeds with no type errors. Smoke-tested live against the real database:
`/api/health`, `/api/lab/dashboard`, `/api/friend-picks/report`,
`/api/paper-trades/report`, `/api/profit/gates`,
`/api/provider/bookmaker-coverage`, `/api/provider/feed-comparison` all
respond correctly, including a full real friend-pick create → auto-resolve
→ auto-score cycle and a real bookmaker coverage scan (bet365 WORKS,
FanDuel EMPTY, 10 real API calls used).

## Migration notes

Additive-only, using the existing v0.3.5 self-healing column migration
(`database.py::_migrate_add_missing_columns`). **Found and fixed a latent
bug in that migration during this release**: schema *inspection* was
interleaved with an open write transaction, which could self-deadlock
against SQLite's rollback-journal locking (a single process's own
inspector connection blocked by its own pending writer) — this is what
caused an earlier "database is locked" failure and left
`Settings.paper_stake_usd` added-but-not-backfilled (NULL) on the real DB.
Fixed by fully separating the inspection pass from the write-transaction
pass, and made the backfill pass unconditional (self-healing on every
startup, not just at the moment a column is first added) so this class of
partial-failure state repairs itself. Verified: 3 consecutive backend
restarts with zero lock errors, and `paper_stake_usd` correctly repaired
to 100.0 with matches/odds/prediction_ledger/prediction_reality row counts
unchanged throughout.

## Version bump

`backend/app/main.py` (FastAPI app version + `/api/health`),
`frontend/package.json`, `frontend/src/App.tsx` sidebar badge — all set to
`0.3.6-profit-validation`.

## Not touched / not cut

All 6 build-order modules were completed; nothing was cut from the bottom
of the list.
