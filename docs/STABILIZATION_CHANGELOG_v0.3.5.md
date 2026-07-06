# v0.3.5 — Provider Execution Fix

Follow-up to the first real BetsAPI validation run (v0.3.4). That run proved
ingestion works but surfaced three concrete problems: FanDuel wastes odds-call
budget on esoccer, Prediction Lab scoring never runs because nothing feeds
finished results back into tracked matches, and first-live capture usually
misses the 15s target under full production load. This release fixes the
execution/scheduling problems only. No betting thesis change, no ML, no new
fake/demo data, no `.env` changes.

## 1. bet365-only by default

- `Settings.sportsbooks_tracked` default changed from `["fanduel", "bet365"]`
  to `["bet365"]` (`models.py`). FanDuel is a verified no-op for esoccer
  markets (empty response, not an error) — see the v0.3.4 validation report.
- Existing installs: `database.py`'s `init_db()` now does a one-time,
  narrowly-scoped correction — if (and only if) a Settings row still holds
  the exact old shipped default, it's moved to the new default. A row a user
  already customized (e.g. added a third book) is left untouched.
- FanDuel remains fully supported as a source string — add it back any time
  from Settings if BetsAPI ever lists esoccer coverage for it.
- New: `data_health()` (`engines/shadow.py`) now warns when any tracked
  sportsbook has a ≥90% empty-response rate over its last 5+ odds calls,
  computed from stored raw payloads via the new
  `betsapi_provider.sportsbook_empty_stats()` helper — surfaces automatically
  on the Data Health page, no separate toggle needed.

## 2. Ended-results ingestion (the scoring blocker)

v0.3.4's poller only ever called `fetch_upcoming`/`fetch_inplay`. Nothing
called BetsAPI's ended-results endpoint and fed a finished score back into a
tracked `Match` row, so `Match.home_score`/`away_score` never got set and
Prediction Lab's `score_predictions()` skipped every frozen prediction
forever — confirmed in the v0.3.4 report (100% of reality rows carried a
`missing_result` warning).

- New `services/poller.py::ingest_ended_results(db, provider, tracked)`:
  calls `provider.fetch_results()`, scopes to tracked leagues, and upserts
  scores into **existing** matches only via the existing `upsert_match()`
  (`routers/data.py`) — which was already null-safe (only sets a field when
  the incoming value is not `None`), so this can never blank a real score.
  Ended events with no matching tracked `Match` are reported as unmatched,
  never silently inserted as a new match.
- Wired into `poll_loop`, throttled to once/45s, running after the
  odds-polling loop so it can never delay a first-live odds fetch.
- After ingestion, `score_predictions()` runs automatically — confirmed live:
  in one controlled run, `pending_scores` dropped from 208 to 106 and
  `scored_predictions` went from 0 to 184 the moment real results started
  flowing in.

## 3. Result-ingestion report

- `GET /api/provider/result-ingestion-report`: `ended_events_fetched`,
  `in_tracked_leagues`, `matched_to_existing_matches`, `scores_updated`,
  `unmatched_ended_events` (+ a sample of unmatched ext_ids),
  `predictions_newly_scored`, `scoring_errors`.
- Surfaced on the Data Health page (Health.tsx).

## 4. First-live priority polling

v0.3.4 treated every tracked match equally each tick, so a match nearing (or
just past) kickoff wasn't served any faster than one 25 minutes out.

- New `_match_priority(match, now, live_missing_first_live)` in
  `services/poller.py` sorts the tracked window into 5 tiers before polling:
  0. live and still missing its first-live snapshot
  1. within ±30s of kickoff
  2. within 2 minutes of kickoff
  3. already tracked pre-match (polled before)
  4. distant upcoming (never polled)
- `MAX_MATCHES_PER_TICK` (60) hard-caps how many matches get an actual
  `fetch_odds()` call in one tick — priority order decides who gets a slot,
  so a large tracked set can no longer starve a match that just went live.
- `cadence_seconds()` and `process_snapshots()` (the actual cadence/thesis
  math) are unchanged — only match *selection order* changed.
- Ended-results ingestion and the Prediction Lab cycle both run after the
  odds-polling loop within a tick, and are throttled (45s / 20s), so neither
  can delay first-live capture.
- With the bet365-only default, the `for book in books` loop now makes one
  call per match per tick instead of two.

## 5. Poller performance metrics

`GET /api/provider/performance-report`: loop duration, odds calls in the
last minute, active tracked matches, first-live candidate count, average and
p95 first-live latency, percent of captures within 15s, API calls by
endpoint (last hour) and by sportsbook, and empty-response rate by
sportsbook. Latency figures are computed from the existing
`PredictionReality.first_live_after_s` column (already populated by
`capture_reality_for_match`); call-volume figures are computed from stored
`RawProviderResponse` rows, not in-memory counters, so they're accurate
regardless of process restarts. Surfaced on the Data Health page.

## 6. First-Live Validation Mode

- New `Settings.validation_mode_enabled` / `Settings.validation_max_matches`
  (default off / 5). When enabled, the poller narrows its tracked window to
  the N soonest-kickoff matches only (across whatever leagues are already in
  `tracked_leagues` — narrow that list too if you want 1–2 leagues
  specifically), instead of the full production set.
- Toggle available in Settings UI, or via `PUT /api/settings`.
- Verified live: with `validation_max_matches=3`, `active_tracked_matches`
  in the performance report dropped to exactly 3 within one tick.

## 7. Tests

New `backend/tests/test_provider_execution_fix.py` (7 tests, all passing):
ended-result ingestion updates scores; ingestion never overwrites a real
score with null; an unmatched ended event is reported, not silently
inserted as a new match; a frozen prediction's `pending_scores` count drops
after result ingestion; FanDuel-style empty responses are counted and
surfaced as a warning without raising; first-live priority tiers sort in the
documented order; seed data and the bet365-only default are off/correct out
of the box.

## Migration notes

- `database.py::_migrate_add_missing_columns()` — additive-only, SQLite
  `ALTER TABLE ... ADD COLUMN` for any model column not yet on disk, with a
  scalar-default backfill for existing rows. Never drops or rewrites a
  column. Verified against the live v0.3.4 database: `matches`,
  `odds_snapshots`, `prediction_ledger`, `prediction_reality`, and
  `raw_provider_responses` row counts were identical before and after
  restart.
- New columns: `raw_provider_responses.sportsbook` (nullable — old rows
  predate per-book tracking and are correctly left unattributed),
  `settings.validation_mode_enabled`, `settings.validation_max_matches`.

## Not touched

Betting/decision logic, ML, `cadence_seconds()`'s timing table,
`process_snapshots()`'s event semantics, `.env`, and no fake/demo data was
added anywhere.
