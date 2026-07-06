# Database Schema

SQLite via SQLAlchemy ORM. Every model maps 1:1 to PostgreSQL — no SQLite-specific
types are used, so the migration path is `DATABASE_URL=postgresql://... alembic upgrade head`.

## players
Identity table for pseudonymous ESoccer players. Names like "Kray (Arsenal)" are split:
the *player* is the persistent identity, the club skin is cosmetic.

| column | type | notes |
|---|---|---|
| id | int PK | |
| name | text UNIQUE | canonical player handle |
| league | text | e.g. "Esoccer Battle - 8 mins", "GT Leagues - 12 mins" |
| elo | float | current overall Elo (default 1500) |
| attack | float | rolling avg goals scored (last 25) |
| defense | float | rolling avg goals conceded (last 25) |
| matches_played | int | |
| created_at / updated_at | datetime | |

## matches
| column | type | notes |
|---|---|---|
| id | int PK | |
| ext_id | text UNIQUE nullable | provider match id (dedup key for imports) |
| start_time | datetime indexed | |
| league | text | |
| home_player_id / away_player_id | FK players | |
| home_score / away_score | int nullable | null = not finished |
| ht_home_score / ht_away_score | int nullable | |
| winner | text | 'home' / 'away' / 'draw' / null |
| duration_min | int nullable | |
| source | text | 'csv', 'betsapi', 'manual', 'seed' |
| created_at / updated_at | datetime | |

## odds_snapshots
Append-only time series. Never update a row — insert a new snapshot.

| column | type | notes |
|---|---|---|
| id | int PK | |
| match_id | FK matches, indexed | |
| sportsbook | text | |
| market | text | 'ML_3WAY', 'SPREAD_2WAY', 'TOTAL' |
| selection | text | 'home' / 'draw' / 'away' / 'over' / 'under' |
| line | float nullable | -0.5, 5.5, etc. |
| american_odds | int | |
| decimal_odds | float | derived, stored for query speed |
| implied_prob | float | 1/decimal, WITH vig |
| is_opening / is_closing | bool | flags, set on import or by connector |
| collected_at | datetime indexed | |

## bets
| column | type | notes |
|---|---|---|
| id | int PK | |
| placed_at | datetime | |
| sportsbook | text | |
| league | text | |
| match_id | FK matches nullable | link when resolvable |
| match_label | text | free text fallback "Kray vs Boki" |
| selection | text | |
| opponent | text | |
| market | text | |
| line | float nullable | |
| american_odds | int | |
| decimal_odds | float | |
| stake | float | |
| result | text | 'win'/'loss'/'push'/'void'/'open' |
| payout / profit | float | |
| closing_american_odds | int nullable | for CLV |
| clv_pct | float nullable | (dec_placed / dec_close − 1) after de-vig, computed |
| model_prob | float nullable | model prob at time of bet, if logged |
| ev_at_placement | float nullable | |
| notes | text | |
| screenshot_ref | text nullable | filename/URL only, no upload handling in MVP |

## predictions
| column | type | notes |
|---|---|---|
| id | int PK | |
| match_id | FK matches | |
| model | text | 'elo_davidson_v1', 'logreg_v1' |
| p_home / p_draw / p_away | float | sums to 1 |
| fair_home / fair_draw / fair_away | float | 1/p decimal odds |
| confidence | float | 0–1, from rating sample size + rating gap stability |
| features_json | text | JSON blob of inputs (for audit + feature importance) |
| created_at | datetime | prediction time — used to prevent lookahead in backtests |

## rating_history
Snapshot of a player's Elo *before* each match. This is what makes lookahead-free
backtesting possible.

| column | type |
|---|---|
| id | int PK |
| player_id | FK players |
| match_id | FK matches |
| elo_before / elo_after | float |
| ts | datetime |

## alerts
| column | type | notes |
|---|---|---|
| id | int PK | |
| match_id | FK | |
| market / selection / line | | |
| sportsbook | text | |
| book_american / book_decimal | | |
| model_prob / fair_decimal | float | |
| ev_pct | float | |
| suggested_stake | float | |
| reason | text | short feature summary |
| status | text | 'open' / 'expired' / 'taken' / 'dismissed' |
| created_at | datetime | |

## settings (singleton row, id=1)
starting_bankroll, unit_size, max_bet_size, min_ev_pct (default 5.0),
kelly_fraction (default 0.25), max_daily_loss, max_weekly_loss,
max_drawdown_shutdown_pct, sportsbooks_tracked (JSON), markets_tracked (JSON),
discord_webhook_url, telegram_bot_token, telegram_chat_id.
API keys are **not** stored in the DB — environment variables only (`.env`).

## backtests
Stores config JSON + results JSON + bankroll curve JSON so past runs are reproducible.

---
## v0.3 real-mode additions
New tables: player_aliases, recommendations, execution_logs, picks, hypotheses,
pattern_notes, strategies, market_events, raw_provider_responses.
New columns: data_source + verification_status on players/matches/odds/bets;
phase + seconds_to_kickoff on odds_snapshots; recommendation_id + ext_id on bets;
include_seed_data / exec_window_seconds / poller_enabled / min_verified_history /
min_similar_sample on settings. Authoritative definitions: backend/app/models.py.
