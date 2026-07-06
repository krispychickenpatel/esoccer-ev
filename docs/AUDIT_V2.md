# AUDIT_V2.md — audit of v0.1 against the v2 spec

## Works today (verified by tests + live smoke test)
- Odds math (american↔decimal, de-vig, EV, Kelly, CLV) — 9/9 pytest green
- Elo + Davidson 3-way with draw support, rating_history, anti-lookahead replay
- Backtester (no lookahead, flat/kelly, drawdown/streak/buckets, bankroll curve)
- CSV import for matches/odds/bets + exports; prediction generation; EV scan
- Alerts + Discord/Telegram webhooks; Settings; Dashboard with risk windows
- React terminal UI: 9 pages, builds clean

## Missing vs v2 spec (all NEW work)
- Pick Engine + Best Picks / Pick History pages, statuses, reason codes, ranking
- Recommendations + Execution Log models, CSVs, importers, timing/latency fields
- Market Movement engine (first-live jump, volatility, per-player/league movement)
- Player identity: canonical ids + alias resolution ("Arsenal (CRUSADER)" → CRUSADER)
- Shadow Model analytics + consensus scoring + Shadow dashboard
- Ensemble signals (elo/form/h2h/movement/shadow/league) with per-signal output
- Research Notebook (hypotheses auto-tested), pattern scan → proposed notes
- Calibration engine, drift detection, similar-setup search, pick grading A+–F
- Data Health panel; data_source + verification_status on every row; seed split
- Seeded startup from your reconstructed evidence; seed review/edit
- Odds Polling Service (adaptive cadence) + MarketEvent lifecycle tracking
- BetsAPI provider with retries, raw response storage, status endpoint, and capability report
- Strategy profiles; model/feature versioning on picks; What-Changed engine

## Files changed
- `app/models.py` — columns added (Match/OddsSnapshot/Bet/Settings/Player) + 8 new tables
- `app/connectors/csv_connector.py` — new templates, aliases, dry-run validation
- `app/connectors/betsapi_provider.py` — real BetsAPI provider + capability report
- `app/routers/{data,bets,admin}.py` — new columns, data-health, seed toggle
- `app/engines/{ratings,backtest}.py` — winner-only Elo, price_mode
- `app/seed.py` — wipe-only real-mode utility; synthetic demo generation removed

## Files added
- `app/engines/{identity,movement,signals,pick_engine,research,shadow,intel}.py`
- `app/routers/{recs,picks,research}.py`
- `app/services/poller.py`, `app/seed_manual.py`
- `frontend/src/pages/{Picks,Recs,Research,Shadow,Health}.tsx`
- packaged seed/sample CSVs removed in v0.3.2-real

## Risks (top 5)
1. Seed evidence may reflect insider info — legal/account risk sits with the bettor,
   not the model. The app measures; it cannot launder the source of an edge.
2. Identity collisions if two operators share a nickname (D4).
3. Movement analytics are empty until timestamped odds exist (D9).
4. SQLite under a 1 Hz poller × many matches will need WAL/Postgres.
5. Small-sample hypothesis "confidence" is noise; every stat ships with n.
