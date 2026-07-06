# v0.3.6.1 — Audit Fix Cleanup

Focused repair pass fixing every issue found in `notes/v0.3.6-audit-report.md`.
Not a feature build. No advanced ML, no betting automation, no bankroll/risk
rule changes, no secrets, no fake/demo/sample production data, no external
bookmaker scans. All schema changes additive and SQLite-safe (in fact, this
release needed zero new columns/tables). All existing data preserved.

## 1. Signal gate sample inflation — fixed

`profit_gates._steam_sample()` now returns raw rows tagged with
`(match_id, selection, sample_time)`; a new `_dedup_by_match_selection()`
collapses repeated horizons of the same (match, selection) into one value
(picking the latest `sample_time` per group, deterministic). `signal_gate()`
now reports both `raw_rows` and `distinct_samples`, and the gate's `n` /
threshold / accuracy / baseline all use `distinct_samples` only. Verified
live: model signal gate now reads `n=174` (`distinct_samples`), `raw_rows:
446` — both numbers shown, no more silent 2.6x inflation. Gate still PASSes
at the honest count (174 ≥ 30). Baseline is computed over the same
deduplicated set for a fair, non-doubly-inflated margin. Frontend
(`ProfitReadiness.tsx`) now shows `distinct_samples` and `raw_rows` side by
side with an explanatory note, so the dashboard can't overstate sample size
again.

## 2. Friend-pick error bucket bug — fixed

Replaced the dead/wrong ternary (`"OK"` branch, unreachable
`BOOK_UNAVAILABLE`) with a single pure decision function,
`_classify_friend_error_bucket()`, exhaustively covering exactly the 7 spec'd
buckets with no escape hatch:

```
book_check == "unavailable"        -> BOOK_UNAVAILABLE
winner_correct is None              -> DATA_UNAVAILABLE
not winner_correct, steam right     -> STEAM_RIGHT_RESULT_WRONG
not winner_correct, steam not right -> WRONG_SIDE
winner_correct, entry didn't survive-> MISSED_EXECUTION_WINDOW
winner_correct, negative CLV        -> CORRECT_SIDE_BAD_PRICE
winner_correct, everything else     -> RESULT_RIGHT_NO_MARKET_EDGE
```

`RESULT_RIGHT_NO_EDGE` renamed to `RESULT_RIGHT_NO_MARKET_EDGE` per the fix
spec. `"OK"` can no longer appear -- confirmed by an exhaustive test over
every combination of inputs.

## 3. `BOOK_UNAVAILABLE` — wired (Option A)

New `_check_book_coverage(db, book_seen)`: matches `book_seen` (free text)
against scanned `BookmakerCoverage` rows by case-insensitive substring.
Returns `"verified"` (matched, status WORKS), `"unavailable"` (matched, any
other status -- real, proven), or `"unknown"` (blank book_seen, no scan has
ever run, or this specific book was never scanned). **Never guesses**:
`"unknown"` always falls through to `DATA_UNAVAILABLE`, never
`BOOK_UNAVAILABLE`, unless a scan has positively shown the book doesn't
work.

## 4. Friend-pick `verify_integrity` — added

New `friend_picks.verify_integrity(db)` + `GET /api/friend-picks/verify-integrity`
(registered before `/{pick_id}` to avoid route-matching collision, same
pattern already used for `/report`). Response: `checked`, `valid`,
`invalid`, `invalid_ids`, `hash_fields_v1`, `hash_fields_v2`, and a `caveat`
explaining the v1/v2 dual-acceptance (see #5). Mirrors
`PredictionLedger.verify_integrity` in spirit.

## 5. Hash field coverage — v1/v2, no data invalidated

`_freeze_payload_v2()` now covers every user-entered field:
`odds_at_pick_american`, `reason`, `confidence`, `provider_event_id` added
to the original 9 fields. `create_friend_pick()` (and therefore
`correct_friend_pick()`, which calls it) hashes every new row under v2.
`verify_integrity()` accepts a row as valid if it matches **either** the v2
or the original v1 reconstruction -- no schema/version column needed, no
existing row is invalidated, and no legacy row breaks app startup (tested
directly: a hand-constructed v1-hashed row verifies clean).
`_freeze_payload` is kept as a backward-compatible alias for `_freeze_payload_v1`.

## 6. `book_seen` scoring assumption — labeled, not fixed by guessing

`pick_out()` now computes and exposes (never stored, always derived at read
time): `scoring_price_source` (the book whose reality data actually backed
the score), `is_reference_feed_proxy` (true whenever the scoring book
differs from what `book_seen` claims -- effectively always true today, since
only bet365 reality data exists), and `book_verified_for_execution` (true
only if `BookmakerCoverage` proves that specific book both `WORKS` and is a
non-reference `execution_candidate` -- bet365 itself can never satisfy this,
by design). Frontend (`FriendPicks.tsx`) shows a "proxy" / "verified" badge
next to the scored price source.

## 7. Production test friend-pick id=1 — documented, not touched

No production data was modified. Added a **read-only, non-stored**
diagnostic (`likely_test_artifact` in `pick_out()`): true when a pick's
`created_at` is more than 60 minutes after its own `kickoff_time` --
strongly suggestive of a retroactively-entered test pick rather than a real
live one. This is a heuristic display flag only; it does not exclude
anything from gates or reports automatically. Pick id=1 (ALIBI vs TUSK,
entered by the assistant during v0.3.6 testing) now shows this flag in the
UI/API. See the audit-fix report for Kris's decision options.

## 8. Generated files removed from git tracking

`.gitignore` now also ignores `*.log`, `*.tsbuildinfo`, `backend/*.log`,
`frontend/*.log`. Ran `git rm --cached` on `backend/uvicorn.dev.log`,
`frontend/vite.dev.log`, `frontend/tsconfig.tsbuildinfo` -- untracked only,
local files untouched.

## Verification

- `pytest`: **70/70 passed** (48 pre-existing + 22 new in
  `tests/test_audit_fix_v0361.py`, plus 1 pre-existing test updated to
  reflect v2 hashing).
- `npm run build`: clean, no TypeScript errors.
- Live smoke test against the real database (no bookmaker scan run):
  `/api/health`, `/api/profit/gates`, `/api/friend-picks/report`,
  `/api/friend-picks/verify-integrity`, `/api/paper-trades/report` all
  return correct data. `signal_gate_model` now shows `n=174,
  distinct_samples=174, raw_rows=446` live, not just in tests.
- `GET /api/lab/verify-integrity`: still 653/653, 0 mismatched -- this
  release touched nothing in the model prediction ledger.
- All existing data preserved: matches (281), odds_snapshots (4403),
  prediction_ledger (653), prediction_reality (398), friend_picks (1),
  paper_trades (6) row counts identical before/after this release.

## Not done / explicitly out of scope

No advanced ML added. No betting automation added. No bankroll/risk rule
changed. No bookmaker coverage scan run (not needed for these fixes). No
move to v0.3.7 — this is a repair pass only.
