# v0.3.7C — Workday Autopilot, Daily Research Loop, Auto Paper Simulation

Date: 2026-07-08/09. Baseline: v0.3.7B.

No live betting, no bet placement automation, no bankroll automation, no
model promotion happened anywhere in this release. Additive schema only
(one code fix cleared two existing `Settings` fields on auto-shutoff;
no new/renamed/removed columns beyond v0.3.7B's).

## Standing self-challenge (completed before implementation)

See the full 7-point self-challenge in-conversation. Headline conclusions
that shaped the build: reuse the existing `poll_loop` throttled-cycle
pattern instead of inventing a new scheduler; every report section checks
its own sample-size gate before computing anything expensive; expect most
of today's forward numbers to legitimately read "pending" on day one.

## Part 0 — Health status v2 (the caveat that started this release)

v0.3.7B's `/api/ops/health` could report `status=OK` while
`collector_alive=false`, `snapshots_created_today=0`, and the last odds row
was hours stale — tolerable for a dashboard a human is watching, actively
misleading for unattended collection. Replaced with four levels
(`OK`/`IDLE`/`DEGRADED`/`FAIL`), `reason_codes`, `next_required_action`,
`expected_collection_window_active`, `collector_expected_alive`, and
`last_successful_poll_at`/`last_successful_ingest_at`/
`last_availability_heartbeat_at`. New `app/workday_config.py` reads
`WORKDAY_*` env vars (collection window, disk/staleness thresholds,
densified-polling gate) with safe always-on defaults if unset.

## Part 1 — Workday Autopilot

- `Settings.autopilot_max_runtime_minutes` / `autopilot_started_at`
  (additive): `poll_loop` auto-disables `poller_enabled` once the cap
  elapses, independent of any external monitor process, and now also
  clears its own bookkeeping fields at that point (fixed after the trial
  run left a stale `autopilot_started_at` that made later status checks
  report a nonsensical negative "minutes remaining").
- New throttled cycle in `poll_loop` (every 15 min): `paper_trade.simulate_all`
  + `execution_classifier_v2.classify_all` + `closing_records.build_all`,
  wrapped in try/except like the existing lab/friend-pick cycles — never
  blocks odds polling.
- `scripts/ops/run_workday_autopilot.py` — validates `BETSAPI_KEY`/
  `BETSAPI_TOKEN` presence (never prints the value), confirms DB writable,
  confirms v0.3.7B schema fields exist, attaches to an already-running
  backend or starts one (`--caffeinate` for full unattended days), sets the
  bounded runtime cap, and monitors health every 60s with heartbeat/incident
  logging.
- `scripts/ops/autopilot_status.py`, `scripts/ops/backup_db.py` (timestamped,
  retained, git-ignored backups — parses its own filename timestamp for
  age/ordering rather than filesystem mtime, see bug note below),
  `scripts/ops/generate_workday_report.py`, `scripts/ops/run_daily_cycle.py`
  (one-command: health → backup → status → workday report → research loop
  → paper sim → combined summary), and a launchd **template** (never
  auto-installed).
- Densified polling requires **both** `Settings.densified_polling_enabled`
  (existing, v0.3.7B) **and** `WORKDAY_ENABLE_DENSIFIED_POLLING=true` (new,
  explicit operational gate) — still off by default; not enabled during
  this release's trial run.

## Part 2 — Daily Research Loop / Auto Paper Simulation

`scripts/research/generate_daily_research.py` (sections A–J: data quality,
execution learning, CLV learning, baseline comparison, steam/price-movement
learning, friend-pick learning, 3 ranked hypotheses, append-only experiment
backlog, self-challenge, one final recommendation) and
`scripts/simulations/run_daily_paper_sim.py` (historical replay/DEGRADED,
forward-clean, CLV-first, entry-timing delay table, market availability,
friend shadow sim, one final verdict from a fixed 7-value enum). Both
compose existing, already-tested engines rather than re-deriving logic.

## Bugs found and fixed during this release (real, not hypothetical)

1. **`shutil.copy2` preserves the source's mtime on the backup copy** —
   `generate_workday_report.py`'s backup-age computation was reading "how
   long ago was `esoccer.db` last modified," not "how long ago was this
   backup made." Fixed by parsing the timestamp encoded in the backup's own
   filename instead of relying on filesystem mtime.
2. **`.env` not loaded before env-var validation** — `run_workday_autopilot.py`
   checked `os.environ` for `BETSAPI_KEY` before anything had imported
   `app.database` (which is what actually triggers `load_dotenv()`), so a
   correctly-configured `.env` looked "missing." Fixed by importing
   `app.database` first.
3. **Per-process `STATUS` dict read from the wrong process (the big one)** —
   `services/poller.py`'s `STATUS` is a plain module-level dict, valid only
   inside the process actually running `poll_loop`. Every new script that
   called `routers.ops.health()` in-process (`run_workday_autopilot.py`'s
   monitor loop, `autopilot_status.py`, `generate_workday_report.py`,
   `generate_daily_research.py`, `run_daily_paper_sim.py`) was reading its
   *own* never-started `STATUS`, not the real backend's — reporting
   `FAIL`/`collector_task_alive=false` while the real backend, in the same
   moment, reported `OK` with genuine quota and heartbeat activity. Caught
   live during the trial run (not by a unit test — the bug only manifests
   when a script attaches to an *already-running* separate backend
   process, which is the normal case for a script like this, not the
   in-memory-db test case). Fixed by having all five scripts prefer the
   real backend's HTTP endpoint (`GET /api/ops/health`), falling back to an
   in-process check only when no backend is reachable at all. Regression
   test added (`test_monitor_loop_checks_health_via_http_not_in_process`).
4. **Data-scope gate label used raw prediction count instead of the
   deduped distinct-sample count** — `run_daily_paper_sim.py`'s top-level
   `data_scope.gate` read `DECISION-GRADE` (n=653 raw predictions) in the
   same report where the CLV/baseline sections correctly used the deduped
   `(match_id, selection)` count (n=231, `EVIDENCE` grade) — an internal
   contradiction. Fixed to use the same distinct-sample count everywhere.
5. **Market-availability hypothesis overclaimed at 0% prevalence** — the
   hypothesis generator asserted "is a real, measurable pattern on bet365"
   as soon as *any* prevalence number existed, including exactly 0%. Real
   trial data (183 combos checked, 0% withdrawn) exposed this immediately.
   Fixed to phrase a zero-prevalence result as an absence finding, not a
   presence finding. Regression test added.
6. **`market_availability.prevalence_report`'s recommendation field claimed
   "borderline (5-10%)" with zero data checked** — found while inspecting
   v0.3.7B's own report before this release even started collecting;
   fixed to say "NO HEARTBEAT DATA YET" when `total_checked == 0`.

## Real bounded trial run (this release's own validation, not a claim)

Ran `scripts/ops/run_workday_autopilot.py --max-minutes 45` against the
live BetsAPI, start to finish, unattended after launch:

- Health stayed `OK` continuously for the full 45 minutes (visible in
  `logs/workday/2026-07-09.jsonl` — one heartbeat line per minute, zero
  `FAIL`/`DEGRADED` entries), then correctly transitioned to `IDLE` the
  instant the cap was reached; the monitor detected the auto-disable and
  exited cleanly on its own.
- 758 poll cycles logged, 933 odds rows collected (**100% with real
  `polled_at`/`ingested_at` system timestamps** — `data_state=CLEAN`),
  891 market-availability heartbeat records, quota consumption 298 of
  3600 (≈8%) over the 45 minutes — comfortably inside budget.
- 183 (match, book, market, selection) combos now have real heartbeat
  history: **0% market-withdrawal/relist prevalence on bet365** — the
  first real evidence (not just the single FanDuel friend-pick anecdote)
  bearing on that question, still too small a sample to be conclusive but
  no longer zero data.
- Forward (system-timestamped) CLV went from **0 samples ("pending")
  to 84 samples** in 45 minutes — `DIRECTIONAL ONLY` (below the 150
  decision-grade floor), avg CLV **-0.47%**, consistent in sign with the
  historical DEGRADED estimate (-0.32%).
- 21 HIGH-quality closing records accumulated (up from 0).
- **Unexpected, surfaced-not-hidden finding**: all 403 forward-trustworthy
  execution classifications produced during the trial came back
  `SIGNAL_TOO_LATE` — every `KICKOFF`-horizon prediction observed was
  frozen at or after the match's actual start, meaning zero real
  pre-kickoff execution window existed for any of them in this window.
  This is a genuine, real pattern this release's own instrumentation
  caught; investigating *why* (freeze-timing vs. horizon-scheduling logic)
  is explicitly out of scope here (no entry-logic changes this release) —
  flagged for the next release's research backlog instead.
- Historical distinct samples grew organically during the trial (231→333)
  as the Prediction Lab kept freezing/scoring in the background; model still
  underperforms the favorite baseline (now -7.6pts on the larger sample,
  vs. -9.5pts before — same direction, not yet enough to call it noise).

## Hard rules confirmed

No live-betting/execution modules referenced anywhere in the new scripts
(grep-tested). `.env`, DB files, backups, logs, and a filled-in launchd
plist are all git-ignored (`.gitignore` extended this release for
`*.db-journal`/`*.db-wal`/`*.db-shm`, `*.plist` with `!*.plist.template`,
and common screenshot/recording extensions). Historical simulations are
always labeled `DEGRADED`; forward simulations are labeled `CLEAN` only
once real system timestamps exist. No sample-size gate was loosened to
manufacture a stronger-looking result.

## Tests / build

**139/139 backend tests pass** (118 pre-existing this session + 21 new
this release). `npm run build` clean.
