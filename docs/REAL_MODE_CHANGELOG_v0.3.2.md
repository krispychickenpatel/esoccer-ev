# Real Mode Change Log — v0.3.2

## Recommendation
Use this build as the baseline for live/paper validation. Do not add advanced ML until BetsAPI odds resolution and first-live capture are proven.

## Changes shipped

- Removed packaged `data/samples/` and `data/seed/` CSVs from the zip.
- Removed `betsapi_stub.py`.
- Replaced `backend/app/seed.py` with a wipe-only real-mode utility.
- Kept manual screenshot seed loader quarantined behind `AUTO_LOAD_SEED_DATA=1` only.
- Added `backend/app/engines/steam.py`:
  - estimates first-live odds movement before kickoff
  - excludes seed/demo sources
  - uses only historical matches before the prediction timestamp
  - outputs current odds, predicted first-live odds, steam probability, expected movement, maximum entry, and execution window
- Added steam fields to Best Picks cards.
- Added `/api/steam/report` and `/api/steam/match/{match_id}`.
- Added BetsAPI capability report via `/api/provider/capability-report`.
- Added Data Health UI table for provider capabilities.
- Updated poller cadence copy to match the actual quota-aware cadence.
- Updated app and API version to `0.3.2-real`.
- Cleaned generated artifacts from the zip: `node_modules`, `dist`, `__pycache__`, `.pytest_cache`, local SQLite DB, TS build info.

## Validation performed

- Backend tests: `22 passed`.
- Frontend production build: passed after `npm ci`.
- Smoke-tested endpoints:
  - `/api/health`
  - `/api/provider/capability-report`
  - `/api/steam/report`
  - `/api/picks/best`

## Known risks

- The steam predictor will intentionally show weak/unknown confidence until the poller captures real pre-live + first-live odds pairs.
- BetsAPI spread/handicap parsing is still marked parser-risk until raw payloads confirm field names.
- Official GT and Official ESportsBattle connectors are not implemented; the capability report marks them unsupported.
- Existing local DBs may still contain old demo/seed rows. Run **Matches → Real mode clean** or `python -m app.seed --wipe`.
- Frontend dev dependency audit reports Vite/esbuild dev-server advisories. Production dependency audit was clean with `npm audit --omit=dev`.

## Next action

1. Add `BETSAPI_KEY` in backend `.env`.
2. Run `/api/provider/capability-report?probe=true` once.
3. Inspect Data Health and raw provider responses.
4. Pull schedule.
5. Pull odds around kickoff.
6. Confirm first-live snapshots exist.
7. Paper trade until the steam report has enough verified first-live pairs.
