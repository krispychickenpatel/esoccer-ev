# ESoccer EV — Prediction Lab Terminal (v0.3.4)

This is an ESoccer quantitative research platform, not a generic pick app.

The system separates five jobs:

1. **Prediction Engine** — estimate true outcome probability.
2. **Decision Engine** — compare model probability with sportsbook price.
3. **Execution Engine** — decide whether the edge can still be captured in the live window.
4. **Risk Engine** — size or block exposure.
5. **Evaluation Engine** — validate whether reality supported the thesis.

The core standard is profitable execution after realistic latency, not impressive-looking predictions.


## What changed in v0.3.4 (stabilization pass)

Bug-fix and hardening release. No new strategy, no new ML — same thesis, cleaner evidence.

- **Fixed feature leakage in the Steam Predictor.** History pairs were bounded by wall-clock time, not prediction time. Any backfilled or late freeze could consume first-live pairs that only settled *after* the prediction moment. `steam_prediction_for_snapshot` now takes `as_of` and the Prediction Lab always passes `prediction_time`.
- **Fixed target leakage in the movement signal.** When freezing after a match already had live snapshots (manual freeze, replay, backfill), the ensemble's movement signal consumed this match's own first-live jump — the exact quantity the prediction is later scored against. `match_timeline` / `movement_metrics` / `movement_signal_for` / `all_signals` now accept an `as_of` cutoff and the Lab passes `prediction_time`.
- **Fixed league-signal lookahead.** League draw-rate context now only counts matches that kicked off before the match being predicted.
- **Fixed live-phase polling cadence.** The poll loop slept a flat 10s, silently capping the documented 2s live bucket at 10s — degrading first-live capture, the core thesis measurement. Sleep is now derived from the tightest active cadence (clamped 1–30s).
- **Throttled the Lab cycle inside the poller** to once per 20s so reality-capture bookkeeping can never delay a first-live odds snapshot.
- **Fixed allow_late horizon contamination.** Late freezes used to stamp *every* passed horizon label (a "T-30m" row created 3 minutes before kickoff), corrupting per-horizon model comparison. Late freezes now produce only the single label nearest to the real seconds-to-kickoff.
- **Added ledger integrity verification.** `GET /api/lab/verify-integrity` recomputes each frozen row's sha256 against `immutable_hash`; the Lab dashboard surfaces any mismatch as a red tamper warning. Frozen means frozen — now it's checkable, not just intended.
- **Fixed UI timestamps.** Backend emits naive UTC datetimes; the frontend parsed them as local time, shifting every displayed time by the viewer's UTC offset. `fmtDT` now treats offset-less ISO strings as UTC.
- **Scoring hardening.** `steam_probability` is None-safe in error bucketing and scoring.
- **Lab dashboard additions.** `pending_scores` total and `ledger_integrity` block.
- **4 new regression tests** covering both leakage fixes, tamper detection, and allow_late labeling (27 total, all passing).

## What changed in v0.3.3 Prediction Lab

- Added a **Prediction Lab** page for self-testing prediction quality.
- Added immutable frozen horizon predictions at pre-kickoff checkpoints.
- Added reality capture for last pre-kickoff odds, first-live odds, closing odds, and result.
- Added scoring for winner accuracy, steam direction, steam magnitude, and entry-window viability.
- Added error buckets: outcome, steam direction, steam magnitude, execution timing, data, and risk.
- Added model comparison by horizon so 30m/15m/10m/5m/2m predictions can be compared objectively.
- Added Gold/Silver/Rejected dataset tiers so training can be restricted to high-quality evidence.
- Integrated the Prediction Lab cycle into the odds poller so every tracked match can become validation data.

## What changed in v0.3.2-real

- Real mode is now the default. Manual/screenshot seed rows do **not** auto-load unless `AUTO_LOAD_SEED_DATA=1` is explicitly set.
- Synthetic demo generation has been removed from the shipped workflow. The old `$10` demo-bet path is no longer part of the normal app.
- Added **Pre-Kickoff Steam Predictor** output on pick cards:
  - current odds
  - predicted first-live odds
  - steam probability
  - expected line movement in price cents
  - maximum entry price
  - execution window
- Added `/api/steam/report` and `/api/steam/match/{match_id}`.
- Added `/api/provider/capability-report` to show which BetsAPI capabilities are working, missing, broken, unsupported, or still unknown.
- Added BetsAPI schedule/odds pulls from the Matches page.
- Added quota-aware poller cadence and first-live event logging.
- Removed packaged sample/seed CSV data from the real-mode zip.

## Run locally

Backend:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal.

## Environment

Set these in `backend/.env` or your shell:

```bash
BETSAPI_KEY=your_key_here
DATABASE_URL=sqlite:///./esoccer.db
AUTO_LOAD_SEED_DATA=0
```

Do not hard-code API keys into source files.

## Real-mode operating order

1. Start backend + frontend.
2. Go to **Matches** → run **Real mode clean** once if you have an old local DB.
3. Set tracked leagues and sportsbooks in **Settings**.
4. Go to **Data Health** and confirm the provider is configured.
5. Go to **Matches** → **Pull BetsAPI schedule**.
6. Run **Pull odds for upcoming** or enable the poller once the schedule is verified.
7. Use **Best Picks** only after odds snapshots exist.
8. Use **Prediction Lab** to freeze/score predictions before trusting Best Picks.
9. Paper trade before staking real bankroll.

## API capability report

`GET /api/provider/capability-report` is safe and reads observed raw provider responses.

`GET /api/provider/capability-report?probe=true` makes real BetsAPI calls. Use only when a key is configured and you are intentionally spending quota.

The report explicitly flags:

- upcoming matches
- live matches
- historical results
- targeted event history
- odds history
- official GT source support
- official ESportsBattle source support

## Steam predictor note

The steam predictor requires verified pre-live and first-live odds pairs. Until the poller captures enough of those, it will correctly show weak/unknown steam confidence. That is the point: no fake certainty.

## Prediction Lab API

```bash
# freeze current due horizons
curl -X POST "http://127.0.0.1:8000/api/lab/freeze-due?allow_late=true"

# capture first-live/reality from stored odds snapshots
curl -X POST http://127.0.0.1:8000/api/lab/capture-reality

# score finished frozen predictions
curl -X POST http://127.0.0.1:8000/api/lab/score

# run all three steps
curl -X POST http://127.0.0.1:8000/api/lab/run-cycle

# verify no frozen prediction has been edited after freezing
curl http://127.0.0.1:8000/api/lab/verify-integrity
```

## Tests

```bash
cd backend
python -m pytest -q

cd ../frontend
npm run build
```

## Data warning

Seed/screenshot reconstruction is quarantined. It can be manually loaded for research review only, but it is not proof of profitability and is excluded from real-mode metrics by default.
