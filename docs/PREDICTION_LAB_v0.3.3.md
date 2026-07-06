# Prediction Lab v0.3.3

This release turns the platform into a self-testing prediction system.

It does **not** add new betting strategy logic. It adds the machinery needed to prove whether future strategy/model changes actually improve prediction and execution.

## Core loop

1. Upcoming match is discovered.
2. The platform freezes predictions at pre-kickoff horizons.
3. Odds reality is captured after kickoff.
4. Final result is captured.
5. The frozen prediction is scored.
6. The miss/win is assigned to an error bucket.
7. Model/horizon comparison reports show what is improving or breaking.

## New tables

- `prediction_ledger` — immutable frozen prediction records.
- `prediction_reality` — last-prekickoff / first-live / closing odds and result reality.
- `prediction_scores` — score and error bucket for each frozen prediction.

## New endpoints

- `GET /api/lab/dashboard`
- `GET /api/lab/predictions`
- `GET /api/lab/model-comparison`
- `POST /api/lab/freeze-due`
- `POST /api/lab/freeze-match/{match_id}`
- `POST /api/lab/capture-reality`
- `POST /api/lab/score`
- `POST /api/lab/run-cycle`
- `GET /api/lab/match/{match_id}`

## New frontend page

- `Prediction Lab`

The page exposes:

- frozen prediction count
- scored prediction count
- Gold/Silver/Rejected reality tiers
- error bucket counts
- model comparison by horizon
- recent frozen prediction records

## Error buckets

- `OK`
- `OUTCOME_ERROR`
- `STEAM_DIRECTION_ERROR`
- `STEAM_MAGNITUDE_ERROR`
- `EXECUTION_TIMING_ERROR`
- `DATA_ERROR`
- `RISK_FILTER_ERROR`

## Dataset tiers

- `gold` — complete pre-live, fast first-live, and result.
- `silver` — usable for research but incomplete/late.
- `rejected` — not safe for training.

Train only on Gold rows once sample size is meaningful.

## Poller integration

When the odds poller is enabled, each provider tick also runs the Prediction Lab cycle:

- freeze due horizon predictions
- refresh reality capture
- score finished predictions

This means every tracked match becomes validation evidence even when no bet is placed.

## Operating rule

Do not promote a new prediction idea because it sounds smart. Promote it only when Prediction Lab shows improvement against the existing model on future/frozen rows.
