# MVP Build Plan, Assumptions, Risks

## Build plan

**Phase 0 — this delivery (working MVP)**
1. SQLite schema + SQLAlchemy models
2. CSV connectors + sample templates (bets, matches, odds)
3. Elo rating engine (Davidson 3-way, K=32, rating history)
4. Prediction engine v1: Elo → p(H)/p(D)/p(A) + confidence
5. EV calculator: de-vig, EV%, flat + fractional Kelly stakes
6. Backtester: date/market/EV/odds filters, bankroll curve, drawdown, streaks
7. Alerts: in-app EV opportunities + Discord/Telegram webhook senders
8. Dashboard: bankroll, P/L, ROI, win rate, avg odds, market breakdown,
   model accuracy (Brier score), average CLV
9. All CSV exports
10. No packaged synthetic data. Empty-state pages must render without fake rows
    on first run

**Phase 1 — you, week 1–2**
- Get real data: subscribe to a feed (BetsAPI esoccer ~US$50/mo tier) or start
  disciplined manual CSV logging of every match + odds snapshot you see
- Log every real bet with closing odds — CLV is your ground truth
- Wipe local research data (`python -m app.seed --wipe`)

**Phase 2 — after ~500+ real matches**
- Fit the Davidson draw parameter ν to your actual league draw rate
- Tune Elo K via backtest grid search (endpoint path preserved; only use after validation)
- Add logistic regression on features_json (scikit-learn path documented)

**Phase 3 — only if Phase 2 shows positive CLV over 200+ bets**
- XGBoost/LightGBM, live odds polling, multi-book line shopping

## Assumptions
- [Certain] One human runs this locally. No auth, no multi-user, SQLite is fine.
- [Likely] ESoccer Battle 8-min matches are the primary league; draw rate ≈ 20–26%.
  Davidson ν defaults to 0.63 (≈24% draw at equal ratings) — refit with real data.
- [Guessing] Player identities are stable across club skins. If a provider keys on
  "Player (Club)", the importer strips the club portion. Verify per provider.
- Manual verified CSV and BetsAPI are the real-mode data sources. Provider capability must be validated before trusting it.

## Risks (ranked)
1. **No data, no model.** The Elo engine is mathematically correct and useless
   until it has ~300+ matches per player pool. Paper-traded/API data demonstrates mechanics,
   nothing more.
2. **Sharp markets.** ESoccer books hold 8–12%. A 5% EV threshold on top of a
   naive Elo will mostly flag model error, not book error. Trust CLV, not ROI,
   for the first 500 bets.
3. **Match integrity.** ESoccer has documented fixing scandals. The model cannot
   see informed money; a too-good price can be a trap. The alert `reason` field
   flags lines that moved sharply against opening.
4. **Small samples lie.** 50 bets tells you nothing (±20% ROI is pure noise).
   The dashboard shows a 95% CI hint on ROI for this reason.
5. **Account limiting.** Winning ESoccer bettors get restricted quickly. Out of
   scope for software; plan for it financially.
6. **Overfitting the backtest.** Every filter you tune on historical data leaks.
   Hold out the most recent 20% of matches and only test final configs on it.

## What data the model needs to be useful
Minimum viable: `match_results.csv` with player names, final scores, start times —
**300+ matches** covering the players you bet on, plus `odds_snapshots.csv` with at
least one pre-match snapshot per match (opening or near-close). 
Good: 2,000+ matches, opening AND closing odds per match (enables CLV + de-vig
comparison), half-time scores.
Gold: tick-level odds history from 2+ books — enables line-movement features,
which in ESoccer are [Likely] more predictive than ratings.

## v0.3.3 Prediction Lab build scope

Approved direction: self-testing prediction infrastructure before advanced ML.

Implemented in v0.3.3:

- Prediction Ledger for frozen horizon predictions.
- Reality Capture for last pre-kickoff, first-live, closing odds, and result.
- Self-Scoring Engine with winner, steam direction, movement magnitude, and entry-window scores.
- Error Buckets to separate model errors from data/execution/risk errors.
- Model Comparison by horizon and version.
- Poller integration so every live-tracked match can generate validation evidence.

Next model work must be evaluated through this loop.
