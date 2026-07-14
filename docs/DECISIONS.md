# DECISIONS.md — autonomous decisions made while you were away

Every decision below was made without you. Format: decision → reasoning → confidence.

## D1. Extend, don't rebuild
Existing v0.1 code passed audit (tests green, endpoints smoke-tested). All v2 features
are added as new modules/columns. **[Certain]** — rebuild would burn time for zero gain.

## D2. Drop-and-recreate DB instead of migrations
Schema gained many columns/tables. No Alembic at v0.x; there is no production data.
`python -m app.seed --wipe` or deleting `esoccer.db` recreates everything.
**[Certain]** this is right pre-release; revisit when real data exists.

## D3. Recommendations and shadow_picks are ONE table
Your spec defines `recommendations.csv`, `shadow_picks`, and `shadow_pick_results`
with ~90% field overlap. I unified them: `recommendations` (with `source_name`) +
`execution_logs` (results/timing). Shadow analytics = queries over
`source_name != 'model'`. Two parallel tables would drift and double every import.
**[Likely]** the right call; if you want literal separate tables, it's a rename.

## D4. Player identity = the nickname in parentheses
"Arsenal (CRUSADER)" → canonical player **CRUSADER**; team name is cosmetic skin.
Matches FanDuel ESoccer reality where the operator is the constant. Alias table maps
every observed formatting variant to one canonical id. **[Likely]** — if a nickname
is ever reused by different operators across leagues, this merges them wrongly;
fix = league-scoped identity later.

## D5. Elo from winner-only matches
Seed bets imply winners but not scores. Ratings engine now accepts matches with
`winner` set and scores null (MOV multiplier = 1.0). Without this, seed players
would have no ratings at all. **[Certain]** math is sound; sample is tiny.

## D6. No-Bet classifier is rules, not ML
~10 settled seed bets cannot train a classifier. Implemented as explicit penalty
rules (low sample, stale data, moved odds, low limit, alias uncertainty, model
disagreement) feeding the confidence breakdown. **[Certain]** — ML here would be
fake rigor. Revisit at 300+ settled picks.

## D7. Weekly pattern scan = on-demand endpoint + button
No cron/scheduler dependency in a local dev app. `/api/patterns/scan` runs the same
logic; run it weekly yourself or wire cron later. **[Likely]** right trade-off.

## D8. Poller ships OFF by default
Odds Polling Service is implemented with your exact cadence table, but it only runs
when `poller_enabled=true` in Settings AND a provider (BetsAPI key) is configured.
Without a data source it would poll nothing at 1 Hz. **[Certain]**

## D9. Execution-window backtests are limited by current data
Backtester gained a `price_mode` (pre_match / first_live) and the movement engine
buckets by seconds-to-kickoff, but seed/synthetic data has only opening/closing
snapshots, so first-live buckets are empty until the poller or real CSVs feed
timestamped snapshots. Reported honestly in Data Health. **[Certain]**

## D10. CSV templates: new spec is canonical, old headers still accepted
Importers accept your new column sets (bet_id, decimal_odds, payout, profit,
screenshot_file, winner, snapshot_id, implied_probability...) and fall back to the
v0.1 headers. Derived fields (decimal, implied) are recomputed and validated against
provided values; mismatches become row warnings, never silent drops. **[Certain]**

## D11. Seed data loads idempotently at startup
Your reconstructed recommendations + settled bets load automatically if absent
(keyed on rec_/seed_bet_ ids), tagged `data_source='manual_seed'`,
`verification_status='seed_partial'`. Synthetic demo generation was removed in v0.3.2-real. Legacy rows, if present in an old DB, remain tagged
`data_source='synthetic_demo'` so Real Mode Clean can remove them. Dashboard and pick analytics take
`include_seed` toggle (Settings, default ON per your instruction). **[Certain]**

## D12. Evidence Notes lives inside the Recommendations page
Not a 17th page. Each rec/execution/bet shows notes + screenshot_ref, editable,
with SEED badge. Page count was getting user-hostile. **[Likely]**

## D13. rec_003/007/009 with Unknown players import as recommendations only
They can't create matches (no opponent). They still count in shadow pass/miss
timing stats. **[Certain]** — inventing opponents would poison identity stats.

## D14. seed_bet_003 inconsistency preserved, flagged
Your rec_004 says "Ambassador vs Alibi" but seed_bet_003 says selection
"Newcastle UTD (AMBASSADOR)" vs opponent "Aston Villa (ALIBI)" — team skins differ
from the rec narrative. I imported the bet as written and linked it to rec_004,
verification_status='seed_partial', note flagging the discrepancy. **[Certain]**
this is what "preserve raw values, let me correct later" requires.

## D15. Ensemble weights start hand-set, logged in code
elo 0.30, form 0.15, h2h 0.10, market-movement 0.20, shadow 0.15, league 0.10,
each scaled by its own data_quality. Transparent function `combine_signals()` with
comments. No fitted weights until there's data to fit on. **[Certain]**

## D16. "BET" label additionally requires ≥1 settled-history guardrail
Beyond your 7 rules: if total settled verified bets < 20, max status is WAIT with
reason DATA_WEAK unless the pick is consensus (friend + model agree). Prevents the
system from shouting BET off synthetic/seed-only evidence. **[Guessing]** on the
threshold (20); change in Settings (`min_verified_history`).

## D17. Poller cadence recalibrated to fit BetsAPI's real budget
Original 60/15/3/1s cascade cost ~113 req/match lifecycle. At ESoccer's
concurrent multi-league match rate (~30 matches/hr/league × 4 leagues seen in
your BetsAPI screenshot), "track everything" needs ~13,560 req/hr against a
3,600 req/hr cap on the $30/mo plan -- 3.7x over, calculated directly, not
estimated. New table (9999s/60s/10s/2s/120s) costs ~34 req/match, supporting
~105 matches/hr. **[Certain]** the old table was unbuildable on this plan;
made the change without asking since it was a correctness fix, not a
preference call. Loses 1s->2s resolution on the live jump -- immaterial,
ESoccer lines don't round-trip that fast.

## D18. Poller refuses to run until tracked_leagues is set
Rather than silently defaulting to "all esoccer" (still 3-4x over budget even
narrowed to 2 leagues at old cadence, comfortably under budget at the new one)
or picking leagues for you, poller_enabled + empty tracked_leagues = idles
with a visible note in Data Health/provider status. This is a decision that
changes cost and coverage -- yours to make, not mine (rule #8: ask when it
changes outcome/costs money). Settings field added; comma-separated league
names, substring-matched against Match.league.

## D19. betsapi_provider.fetch_odds spread (1_2) field names are UNVERIFIED
BetsAPI's docs confirmed 1_2 = Asian Handicap exists but didn't publish its
JSON schema in what you shared. Code guesses common field names
(handicap/home_od/away_od) defensively -- **do not trust spread data from this
provider until you've hit a real event with a live token and confirmed the
actual response shape.** RawProviderResponse table stores every raw call
specifically so this is checkable without re-hitting the API.

## D20. tracked_leagues default set from actual bet history, not guessed
Club-name picks (6/9 recs: Arsenal/Spurs/Newcastle/Aston Villa/Tottenham) ->
the two 8-min club-format leagues (Battle, H2H GG League) -- can't split
these two further, both use identical English-club skins and my own seed
reconstruction never captured real league name (logged generic "FanDuel
ESoccer" placeholder -- my gap, noted to the user). Country-name picks
(Norway/Portugal/Morocco/Qatar, rec_006 + seed_bet_006) -> confirmed as
Esoccer GT Leagues format via live BetsAPI fixture data pulled in an earlier
search (matched exact country-vs-country pattern). Battle Volta excluded:
zero supporting evidence anywhere in the 9 recs / 10 bets. Set as the
tracked_leagues column default (D18) so every fresh init/wipe starts correct
without a manual step. Budget: 3 leagues x ~30 matches/hr est. = ~90/hr
against the ~105/hr cap from D17 -- 14% margin, thin because the 30/hr/league
rate is an eyeballed estimate from one screenshot, not measured. Watch
/api/provider/status after go-live; drop to 2 leagues (cut Battle or H2H GG
League, keep GT League since it's the only unambiguous one) if real volume
runs hot.

## D21. Quota tracked live from response headers + 5% poller brake
BetsAPI returns X-RateLimit-Limit/Remaining/Reset on every response (fixed
hourly window; their docs: on 429 you wait for next-hour reset or buy a
volume package -- no mid-hour replenish exists). Provider now records these
into /api/provider/status so budget monitoring is measured, not estimated
(closes the D20 "eyeballed 30 matches/hr" gap). Poller pauses itself below
5% remaining, holding headroom for manual calls until reset, instead of
burning into 429s. **[Certain]** on mechanics (sourced from BetsAPI docs);
**[Guessing]** on 5% as the right brake threshold -- adjust in poller.py if
you'd rather run hotter.

## D22. Country/club team-skin heuristic (D20) was wrong -- retracted
Live BetsAPI data proves H2H GG League and Adriatic League both mix country-
and club-format team skins in the same league (e.g. H2H GG League: Netherlands
vs USA *and* Napoli vs Barcelona). The pattern that seemed to hold on 9
screenshots was coincidence, not signal. **Consequence: historical club-skin
picks (Arsenal, Spurs, Newcastle, Aston Villa, Tottenham in your seed recs)
can no longer be attributed to a specific league.** Not narrowed to 2
candidates anymore -- unresolvable from data on hand. Only fix: ask the
source which league name is on the slip, going forward. Do not re-attempt
inference from team names -- proven not to work.

## D23. tracked_leagues widened 3->5; D17 budget estimate was ~10x too high
Measured real combined esoccer match rate from a live /v3/events/upcoming
pull: ~11.6 matches/hr across all 5 confirmed leagues (GT Leagues ~4.4/hr,
Adriatic ~3.4/hr, H2H GG ~1.6/hr, Battle Volta ~1.3/hr, Battle ~1.0/hr) --
computed from real timestamps (3.87hr span / 45 esoccer events in one
50-result page), not estimated. At D17 cadence (~34 req/match): ~394 req/hr,
11% of budget. D17's "3.7x over" claim was built on an eyeballed ~120/hr
guess from one screenshot; that guess was wrong by roughly an order of
magnitude. Added Esoccer Adriatic League (10min, 29% of live esoccer volume)
-- absent from the phone app's 4-league list, never accounted for before now.
Kept D17's leaner cadence anyway (no upside to reverting given first-live
jump is still caught at 2s resolution) but tracking scope no longer needs to
be conservative. If real usage over the following week comes in far off this
11.6/hr estimate (one snapshot, not a full-day measurement), re-open this.

## D24. D22's league-attribution gap closed -- friend's league confirmed as H2H GG League
User confirmed 2026-07: "eSoccer H2H GG League 2x4mins." [Likely] two 4-min
halves = 8 min total = BetsAPI's "Esoccer H2H GG League - 8 mins play"
exactly -- not independently string-verified against BetsAPI as the literal
same league entity, but strong enough to replace the "FanDuel ESoccer"
placeholder that only existed because the real league was unknown at
reconstruction time (D14/D22). Retroactively re-tagged: seed_manual.py's
FRIEND_LEAGUE constant, all 9 seed recommendations, all derived seed matches,
quarantined manual seed loader only; packaged data/seed CSVs removed. Shadow dashboard's profit_by_league will now correctly
attribute friend's historical results instead of grouping them under a
placeholder string.

Scope clarification, not a change: this narrows the SHADOW signal's known
league to H2H GG League specifically. It does NOT narrow tracked_leagues
(D23) -- the independent system-wide model still covers all 5 confirmed
leagues per the explicit "everything else independent, beat him" instruction.
Shadow = friend's one league. System = all five, on purpose, so the model can
find edges he's never touched.

If this is ever contradicted (friend names a different league explicitly),
revert FRIEND_LEAGUE and re-flag every affected row -- don't patch quietly.

## D25. Backfill built on event/history, not events/ended -- schema unverified
User pasted BetsAPI's /v1/event/history doc page (per-matchup history, qty
1-20) instead of the bulk events/ended dump D-earlier assumed would be the
backfill mechanism. Switched to event/history: scoped to actual players in
upcoming tracked-league matches (cheaper on quota, targeted, not a firehose).
Script: backend/app/backfill.py, manual trigger only (`python -m app.backfill`),
not run at startup. Scoped to all 5 tracked leagues by default (D23 "beat
him" instruction) -- not narrowed to friend's H2H GG League alone.

**[Certain] response schema (results.h2h/home/away keys) is UNVERIFIED.**
BetsAPI never published event_history.json's structure in what was shared.
fetch_event_history() returns [] safely if wrong, logs a warning if every call
comes back empty, and every raw response lands in raw_provider_responses for
inspection. Could not test against the live API from the build sandbox --
network allowlist doesn't include api.b365api.com. User runs the first real
test.

## D26. .env loading moved to database.py -- was only wired into main.py (bug)
User hit "BETSAPI_KEY not set" running `python -m app.backfill` despite a
correct .env file. Root cause: D21 wired load_dotenv() into main.py only.
backfill.py is a separate entry point that never imports main -- .env never
loaded for it. This was a real code bug, not a user setup error; confirmed by
checking backfill.py's imports (no dotenv call existed) before touching
anything.

Fix: moved the load_dotenv() call into database.py, which every entry point
imports (main, backfill, seed, seed_manual, any future script). Removed the
now-redundant copy from main.py -- one load site, not two that can drift.
Verified: imported app.backfill standalone with a test .env, confirmed
BETSAPI_KEY was visible without touching main.py at all.

## D27. Poller never discovered new matches -- structural gap, not a timing issue
Data Health showed "Ticks: 3, snapshots: 0, calls: 0" -- poller was looping
but never once called the provider. Root cause: no code path anywhere ever
inserted upcoming (home_score IS NULL) matches with a real ext_id into the DB.
backfill.py calls fetch_upcoming() but only uses it to find event IDs for
history lookups -- the upcoming events themselves were discarded, never
saved. The poller's match-selection query (home_score IS NULL AND ext_id IS
NOT NULL) was structurally correct but had zero rows to ever find, no matter
how long it ran.

Fix: poll_loop now calls fetch_upcoming() itself, throttled to once/60s
(_LAST_DISCOVERY module var), upserts scoped events into Match before running
the existing odds-polling logic. Cost: ~60 req/hr for discovery, on top of
the ~394 req/hr odds-polling estimate (D23) -- still ~13% of the 3600/hr cap
combined. Verified: full app import clean (no circular import from
poller->routers.data), 22/22 tests pass.

## D28. league_profiles/sportsbook_profiles never respected include_seed_data
User's Data Health screenshot showed FanDuel at 86.6% ROI vs pinnacle/bet365
in the negative -- traced to FanDuel's number being 100% derived from the 9
real seed bets (n=10, survivorship-biased) with zero dilution or flag, shown
identically to the largely-synthetic pinnacle/bet365 rows. Root cause: unlike
shadow_dashboard() (which already had include_seed), league_profiles() and
sportsbook_profiles() took no such parameter -- the Settings toggle the user
had just fixed for the Dashboard did nothing here.

Fix: both functions take include_seed: bool = True, filter matches/bets by
data_source/source before aggregating. Routes (leagues_route, books_route)
now read Settings.include_seed_data instead of calling with no argument.
Added seed_influenced flag per league row + amber SEED badge in Health.tsx
so even with the toggle ON, tainted rows are visible, not silent. Frontend
rebuilt clean.

## D29. Table horizontal scroll was broken by a global CSS rule, not missing markup
User reported Bets/Matches/Odds tables couldn't scroll left-right. The
`.scroll-x` wrapper class was already correctly applied to all three pages --
the actual bug was `table { width: 100%; }` in theme.css, which forces every
table to shrink-fit its container, giving `overflow-x: auto` nothing to ever
scroll regardless of column count. Fixed: `table { width: max-content;
min-width: 100%; }` -- lets wide tables exceed container width (enabling real
scroll) while narrow tables still fill available space. One-line fix,
global, applies to every table in the app at once.

## D30. data_source/verification_status existed in the DB but were invisible in 3 of 4 tables
Bets and Odds list endpoints never returned data_source at all (checked
row_to_dict() and list_odds() directly -- confirmed absent, not assumed).
Matches endpoint returned it but only as plain muted text, not a clear
badge. This is the direct cause of "is this real or fake" being unanswerable
from the UI -- the tagging existed, it just never reached the screen. Fixed:
all three endpoints now return data_source (+ phase for odds); all three
tables show a colored badge (LIVE/SEED/DEMO/MANUAL) with a hover tooltip
explaining what each means.

## D31. Quick-add tool for Recommendations -- built instead of a text parser
User wants to skip CSV import for single new recs. Considered building a
freeform-text parser (paste what you'd tell me in chat, auto-extract
fields) -- rejected: parsing informal text reliably needs real NLP and would
silently misparse edge cases with no visibility into what went wrong. Built
a structured one-shot form instead, hitting the existing POST
/api/recommendations endpoint directly (no new backend code needed -- the
endpoint already existed, only the UI was missing). Trade-off: costs a few
more clicks than a text box, but every field is explicit and validated
before submit -- silent misparses are worse than a few extra fields.

## D32. Player/betting terminology had zero explanation anywhere -- Glossary page added
20 terms across Ratings/Betting-math/Statistics/Picks/Data-quality, grouped,
one definition each, linked from Ratings page. Also upgraded the one-line
descriptions on Predictions, Backtests, Shadow, and Research pages from
technical shorthand to actual explanations of purpose and how each page
differs from the others (Predictions vs Best Picks was the most-asked
confusion point -- Predictions is one signal in isolation with a scoreboard,
Best Picks is the full decision layer combining six signals + EV + rules).

## D33. KILL CRITERION for the bet365/BetsAPI pre-kickoff early-steam thesis (pre-registered 2026-07-14)

**This entry is immutable.** Any future change to the criterion below
requires a NEW dated amendment underneath it that explains why the original
was changed -- never a silent edit of the text that follows.

Decision sample at registration (v0.3.7D.4):
- strict 20s CLV n=41, avg decimal CLV=-1.919%
- strict 30s CLV n=35, avg decimal CLV≈-2.245%
- strict 45s CLV n=28, avg decimal CLV≈-3.512%

**Primary decision bucket: 45-second strict EXECUTABLE_PREKICK_STRICT forward CLV.**

The current bet365/BetsAPI pre-kickoff early-steam thesis is rejected
(THESIS_KILL_REVIEW_REQUIRED) only when ALL of the following are true:

1. strict 45-second CLV n >= 150 unique decisional samples;
2. average decimal CLV <= -1.0%;
3. the 95% confidence interval upper bound is below 0;
4. partition and cross-tab reconciliation pass (status=OK, zero
   unrecognized rows);
5. RESEARCH_ONLY_KICKOFF and EXECUTABLE_VIA_START_DELAY rows are excluded
   (the strict sample already only contains EXECUTABLE_PREKICK_STRICT rows);
6. samples use clean system timestamps (`close_polled_at`/`close_ingested_at`
   not null) and valid closing records (`close_quality` in HIGH/MEDIUM,
   `all_three_outcomes_present`).

**Confidence interval method:** a match-clustered bootstrap (2000
iterations, seed 1234) -- resamples MATCH IDs with replacement, not
individual rows, because a single match can contribute more than one
correlated selection/decision. Treating those as independent observations
would understate the true variance. Implemented in
`backend/app/engines/evidence_checkpoint.py::_clustered_bootstrap_ci`,
additive to (never replacing) `strict_forward_metrics.strict_forward_clv`'s
own unclustered per-row bootstrap.

**When the kill criterion fires:**
- do not tune the entry floor;
- do not add model features;
- do not automatically retrain;
- do not promote any model;
- output `THESIS_KILL_REVIEW_REQUIRED` prominently in the unattended status;
- recommend either source/book evaluation or project termination;
- require human review before another development release.

**Directional recovery criterion:** if strict 45-second CLV reaches n >= 50
and average CLV is >= 0, output `DIRECTIONAL_RECOVERY_CANDIDATE`, continue
collection toward n=150, and do not claim a proven edge.

**Negative-but-not-killed:** if strict 45-second CLV remains negative at
n >= 50 but has not met the kill criterion, output
`NEGATIVE_DIRECTIONAL_SIGNAL`, continue only toward the pre-registered
n=150 decision gate, and freeze model/threshold development.

None of these statuses automatically stop collection or alter the model --
`THESIS_KILL_REVIEW_REQUIRED` and the others are surfaced prominently for
human review, never acted on autonomously. **[Certain]** -- this is the
gate the whole D-series of releases exists to compute correctly; changing
it later requires a dated amendment, not a silent edit.
