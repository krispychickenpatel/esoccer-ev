# v0.3.3 — Prediction Lab

## Recommendation implemented

Build a self-testing prediction system. Do not add advanced ML or extra betting strategies yet.

## Added

- Backend engine: `backend/app/engines/prediction_lab.py`
- Backend router: `backend/app/routers/lab.py`
- Frontend page: `frontend/src/pages/PredictionLab.tsx`
- New tables:
  - `prediction_ledger`
  - `prediction_reality`
  - `prediction_scores`
- New endpoints:
  - `/api/lab/dashboard`
  - `/api/lab/predictions`
  - `/api/lab/model-comparison`
  - `/api/lab/freeze-due`
  - `/api/lab/freeze-match/{match_id}`
  - `/api/lab/capture-reality`
  - `/api/lab/score`
  - `/api/lab/run-cycle`
  - `/api/lab/match/{match_id}`

## Behavior

- Predictions are frozen at horizons and cannot be silently modified.
- Reality capture grades market data quality as Gold/Silver/Rejected.
- Scoring separates winner accuracy from steam direction, steam magnitude, and execution-window viability.
- Error buckets expose whether misses are caused by model logic, market movement, latency/execution, data gaps, or risk filters.
- Poller now runs the lab cycle after provider ticks.

## Validation

- Backend tests: `23 passed`
- Frontend build: passed
- Smoke-tested:
  - `/api/health`
  - `/api/lab/dashboard`

## Known limitation

Existing local databases will create the new tables on startup, but old rows remain old rows. Use Real Mode Clean if your local DB still contains historical demo/seed contamination.
