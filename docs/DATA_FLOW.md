# Data Flow

```
                        ┌────────────────────────────────────────────┐
                        │                INGESTION                   │
                        │                                            │
  match_results.csv ───▶│  CSVConnector ──┐                          │
  odds_snapshots.csv ──▶│                 │   ConnectorInterface     │
  bet_history.csv ─────▶│                 ├──▶ .fetch_matches()      │
                        │  BetsApiProvider ───┘   .fetch_odds()          │
                        │  (add real APIs by implementing the        │
                        │   same interface — nothing else changes)   │
                        └───────────────┬────────────────────────────┘
                                        │ normalized rows (dedup on ext_id / composite key)
                                        ▼
                        ┌────────────────────────────────────────────┐
                        │              SQLite DATABASE               │
                        │  players · matches · odds_snapshots ·      │
                        │  bets · rating_history · predictions       │
                        └───────┬───────────────────┬────────────────┘
                                │                   │
             finished matches   │                   │  upcoming matches + latest odds
                                ▼                   ▼
                   ┌────────────────────┐   ┌─────────────────────────┐
                   │   RATING ENGINE    │   │    PREDICTION ENGINE    │
                   │  Elo (Davidson 3-  │──▶│  p(H)/p(D)/p(A), fair   │
                   │  way), attack/def, │   │  odds, confidence,      │
                   │  form, H2H, draw%  │   │  features_json          │
                   │  writes rating_    │   └───────────┬─────────────┘
                   │  history rows      │               │
                   └────────────────────┘               ▼
                                            ┌─────────────────────────┐
                                            │      EV CALCULATOR      │
                                            │  de-vig book prices,    │
                                            │  EV = p·dec − 1,        │
                                            │  flat + fractional      │
                                            │  Kelly stake            │
                                            └───────────┬─────────────┘
                                                        │ EV ≥ threshold
                                                        ▼
                                            ┌─────────────────────────┐
                                            │         ALERTS          │
                                            │  in-app + Discord/      │
                                            │  Telegram webhooks      │
                                            └─────────────────────────┘

  BACKTESTER (offline loop):
  replay matches chronologically → rating_history gives Elo *as of* each odds
  snapshot → simulate "would I have bet?" against config → bankroll curve,
  ROI, drawdown, streaks, breakdowns. No lookahead: a prediction for match M
  only uses matches that finished before M's snapshot timestamp.
```

## Anti-lookahead rule (the one that matters)
Every backtest prediction is computed from `rating_history.elo_before`, which is
written at replay time. If you ever swap in an ML model, train it with a rolling
cutoff (walk-forward), never on the full dataset.
