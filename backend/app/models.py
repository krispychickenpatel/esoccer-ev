from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Player(Base, TimestampMixin):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    league: Mapped[str] = mapped_column(String(120), default="")
    elo: Mapped[float] = mapped_column(Float, default=1500.0)
    attack: Mapped[float] = mapped_column(Float, default=0.0)   # avg goals for, last 25
    defense: Mapped[float] = mapped_column(Float, default=0.0)  # avg goals against, last 25
    matches_played: Mapped[int] = mapped_column(Integer, default=0)
    data_source: Mapped[str] = mapped_column(String(30), default="csv_import")
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")


class Match(Base, TimestampMixin):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    ext_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    league: Mapped[str] = mapped_column(String(120), default="")
    home_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    away_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ht_home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ht_away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner: Mapped[str | None] = mapped_column(String(8), nullable=True)  # home/away/draw
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="manual")  # data_source
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")

    home_player: Mapped[Player] = relationship(foreign_keys=[home_player_id])
    away_player: Mapped[Player] = relationship(foreign_keys=[away_player_id])


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    sportsbook: Mapped[str] = mapped_column(String(60))
    market: Mapped[str] = mapped_column(String(30))      # ML_3WAY / SPREAD_2WAY / TOTAL
    selection: Mapped[str] = mapped_column(String(30))   # home/draw/away/over/under
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    american_odds: Mapped[int] = mapped_column(Integer)
    decimal_odds: Mapped[float] = mapped_column(Float)
    implied_prob: Mapped[float] = mapped_column(Float)
    is_opening: Mapped[bool] = mapped_column(Boolean, default=False)
    is_closing: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    phase: Mapped[str] = mapped_column(String(12), default="pre_match")  # pre_match/live
    seconds_to_kickoff: Mapped[float | None] = mapped_column(Float, nullable=True)  # negative = after KO
    data_source: Mapped[str] = mapped_column(String(30), default="csv_import")
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")
    # v0.3.7B: true system-observation timestamps (additive, all nullable).
    # collected_at is UNCHANGED and remains provider event-time (BetsAPI's own
    # add_time) -- see notes/triage/v0_3_7A-census.md Gate G1. source_ts is a
    # same-value alias for collected_at so future code can be explicit about
    # which clock it means without touching the legacy field. polled_at/
    # response_received_at/ingested_at are OUR wall clock, populated going
    # forward by the provider/poller; historical rows keep these NULL rather
    # than a fabricated backfill.
    source_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poll_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("poll_cycles.id"), nullable=True)
    raw_response_id: Mapped[int | None] = mapped_column(ForeignKey("raw_provider_responses.id"), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider_book: Mapped[str | None] = mapped_column(String(60), nullable=True)
    market_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    availability_state: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Bet(Base, TimestampMixin):
    __tablename__ = "bets"
    id: Mapped[int] = mapped_column(primary_key=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    league: Mapped[str] = mapped_column(String(120), default="")
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)
    match_label: Mapped[str] = mapped_column(String(200), default="")
    selection: Mapped[str] = mapped_column(String(120), default="")
    opponent: Mapped[str] = mapped_column(String(120), default="")
    market: Mapped[str] = mapped_column(String(30), default="ML_3WAY")
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    american_odds: Mapped[int] = mapped_column(Integer)
    decimal_odds: Mapped[float] = mapped_column(Float)
    stake: Mapped[float] = mapped_column(Float)
    result: Mapped[str] = mapped_column(String(10), default="open")  # win/loss/push/void/open
    payout: Mapped[float] = mapped_column(Float, default=0.0)
    profit: Mapped[float] = mapped_column(Float, default=0.0)
    closing_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_at_placement: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    screenshot_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ext_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)  # bet_id from CSV
    recommendation_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"), nullable=True)
    data_source: Mapped[str] = mapped_column(String(30), default="csv_import")
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    model: Mapped[str] = mapped_column(String(40), default="elo_davidson_v1")
    p_home: Mapped[float] = mapped_column(Float)
    p_draw: Mapped[float] = mapped_column(Float)
    p_away: Mapped[float] = mapped_column(Float)
    fair_home: Mapped[float] = mapped_column(Float)
    fair_draw: Mapped[float] = mapped_column(Float)
    fair_away: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RatingHistory(Base):
    __tablename__ = "rating_history"
    __table_args__ = (UniqueConstraint("player_id", "match_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    elo_before: Mapped[float] = mapped_column(Float)
    elo_after: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    market: Mapped[str] = mapped_column(String(30))
    selection: Mapped[str] = mapped_column(String(30))
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60))
    book_american: Mapped[int] = mapped_column(Integer)
    book_decimal: Mapped[float] = mapped_column(Float)
    model_prob: Mapped[float] = mapped_column(Float)
    fair_decimal: Mapped[float] = mapped_column(Float)
    ev_pct: Mapped[float] = mapped_column(Float)
    suggested_stake: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open/expired/taken/dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)  # always 1
    starting_bankroll: Mapped[float] = mapped_column(Float, default=1000.0)
    unit_size: Mapped[float] = mapped_column(Float, default=10.0)
    max_bet_size: Mapped[float] = mapped_column(Float, default=50.0)
    min_ev_pct: Mapped[float] = mapped_column(Float, default=5.0)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=100.0)
    max_weekly_loss: Mapped[float] = mapped_column(Float, default=300.0)
    max_drawdown_shutdown_pct: Mapped[float] = mapped_column(Float, default=25.0)
    # v0.3.5: bet365 is the only source that has actually returned esoccer
    # odds in real testing -- fanduel is a verified no-op (empty response,
    # not an error) that just burns API budget every tick. Kept easy to add
    # back per-installation via Settings if BetsAPI ever lists fanduel
    # coverage for esoccer.
    sportsbooks_tracked: Mapped[str] = mapped_column(Text, default='["bet365"]')
    markets_tracked: Mapped[str] = mapped_column(Text, default='["ML_3WAY"]')
    discord_webhook_url: Mapped[str] = mapped_column(Text, default="")
    telegram_bot_token: Mapped[str] = mapped_column(Text, default="")
    telegram_chat_id: Mapped[str] = mapped_column(Text, default="")
    include_seed_data: Mapped[bool] = mapped_column(Boolean, default=False)
    exec_window_seconds: Mapped[int] = mapped_column(Integer, default=30)
    poller_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    min_verified_history: Mapped[int] = mapped_column(Integer, default=20)  # D16 guardrail
    min_similar_sample: Mapped[int] = mapped_column(Integer, default=8)
    tracked_leagues: Mapped[str] = mapped_column(
        Text, default='["Esoccer Battle - 8 mins play", "Esoccer H2H GG League - 8 mins play", "Esoccer GT Leagues \\u2013 12 mins play", "Esoccer Adriatic League - 10 mins play", "Esoccer Battle Volta - 6 mins play"]')
    # v0.3.5: First-Live Validation Mode. When enabled, the poller ignores the
    # full tracked_leagues window and instead tracks only the
    # validation_max_matches soonest-kickoff matches, so first-live capture
    # latency can be measured under controlled (not production) load.
    validation_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_max_matches: Mapped[int] = mapped_column(Integer, default=5)
    # v0.3.6: dollar value of one paper-trade "unit". Paper P/L is reported
    # in units and in USD (units * this). Not a real-money setting.
    paper_stake_usd: Mapped[float] = mapped_column(Float, default=100.0)
    # v0.3.7B: near-kickoff densified polling. Ships OFF by default -- this
    # release builds and unit-tests the scheduler/backoff/heartbeat
    # mechanism but does not change live collection behavior unless
    # explicitly enabled. quota_pct_cap is the hard ceiling (see prompt:
    # "do not exceed 60% of documented hourly quota by design").
    densified_polling_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    densified_polling_quota_pct_cap: Mapped[float] = mapped_column(Float, default=60.0)
    densified_polling_hourly_quota_cap: Mapped[int] = mapped_column(Integer, default=3600)
    # v0.3.7C: Workday Autopilot bounded runtime. autopilot_started_at is set
    # by scripts/autopilot_control.py when a supervised/unattended run
    # begins; poll_loop auto-disables poller_enabled once
    # autopilot_max_runtime_minutes has elapsed -- a belt-and-suspenders
    # safety net independent of anyone remembering to turn it off manually.
    # NULL max_runtime = no cap (ordinary manual poller_enabled behavior,
    # unchanged from before this release).
    autopilot_max_runtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autopilot_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # D22/D23: widened from 3->5 leagues after real BetsAPI data proved two
    # things wrong in D20/D17: (1) country-vs-club team skin does NOT predict
    # league -- H2H GG League and Adriatic League each mix both formats, so
    # historical club-skin picks (Arsenal/Spurs/etc.) can no longer be
    # attributed to a specific league at all; (2) real measured combined rate
    # across all 5 esoccer leagues is ~11.6 matches/hr, not the ~120/hr
    # eyeballed guess D17 was built on -- ~394 req/hr at this cadence, 11% of
    # the 3600/hr cap. Budget supports tracking all 5; narrowing further has
    # no justification. Adriatic League (10min) was invisible in the phone
    # app's 4-league list but is 29% of live esoccer volume -- added.


class BacktestRun(Base):
    __tablename__ = "backtests"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    config_json: Mapped[str] = mapped_column(Text)
    results_json: Mapped[str] = mapped_column(Text)
    curve_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# v2 tables — Pick Engine / Recommendations / Research / Identity / Poller
# ---------------------------------------------------------------------------

class PlayerAlias(Base):
    __tablename__ = "player_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(160), unique=True, index=True)  # raw observed form
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)


class Recommendation(Base, TimestampMixin):
    """A pick from ANY source (friend/model/user). source_name != 'model' rows
    are the Shadow Model population (see docs/DECISIONS.md D3)."""
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(primary_key=True)
    ext_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    source_name: Mapped[str] = mapped_column(String(60), default="friend")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    league: Mapped[str] = mapped_column(String(120), default="")
    home_name: Mapped[str] = mapped_column(String(160), default="")   # raw as observed
    away_name: Mapped[str] = mapped_column(String(160), default="")
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)
    recommended_selection: Mapped[str] = mapped_column(String(160), default="")
    acceptable_markets: Mapped[str] = mapped_column(Text, default='["ML_3WAY"]')  # JSON list
    max_spread: Mapped[float | None] = mapped_column(Float, nullable=True)        # e.g. -0.5
    min_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ideal_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence_label: Mapped[str] = mapped_column(String(20), default="medium")
    stake_plan: Mapped[float | None] = mapped_column(Float, nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    limit_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    # event timeline (all optional; poller/user fill what they can)
    user_ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    slip_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_live_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending/live-ready/placed/missed/rejected/expired/settled/pass
    notes: Mapped[str] = mapped_column(Text, default="")
    screenshot_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    data_source: Mapped[str] = mapped_column(String(30), default="manual_verified")
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")


class ExecutionLog(Base, TimestampMixin):
    __tablename__ = "execution_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ext_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    recommendation_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"), nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    live_detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bet_placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_market: Mapped[str] = mapped_column(String(30), default="")
    actual_line: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_at_slip: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_at_first_live: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stake: Mapped[float | None] = mapped_column(Float, nullable=True)
    accepted_odds_movement: Mapped[bool] = mapped_column(Boolean, default=False)
    was_within_window: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)  # live->placed
    status: Mapped[str] = mapped_column(String(16), default="placed")  # placed/missed/rejected
    missed_reason: Mapped[str] = mapped_column(String(40), default="")
    # odds_moved/market_gone/no_show/late/limit_low/account_unavailable/app_lag
    notes: Mapped[str] = mapped_column(Text, default="")
    data_source: Mapped[str] = mapped_column(String(30), default="manual_verified")
    verification_status: Mapped[str] = mapped_column(String(20), default="user_verified")


class Pick(Base):
    """Pick Engine output. Never overwritten — reproducibility (spec: model versioning)."""
    __tablename__ = "picks"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    market: Mapped[str] = mapped_column(String(30))
    selection: Mapped[str] = mapped_column(String(30))
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    current_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ideal_american_odds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_prob: Mapped[float] = mapped_column(Float)
    fair_decimal: Mapped[float] = mapped_column(Float)
    ev_pct: Mapped[float] = mapped_column(Float)
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(10), default="WAIT")  # BET/WAIT/PASS/MISSED/EXPIRED
    reason_codes: Mapped[str] = mapped_column(Text, default="[]")          # JSON list
    confidence_json: Mapped[str] = mapped_column(Text, default="{}")       # breakdown
    signals_json: Mapped[str] = mapped_column(Text, default="{}")          # per-signal outputs
    suggested_stake: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exec_window_seconds: Mapped[int] = mapped_column(Integer, default=30)
    model_version: Mapped[str] = mapped_column(String(40), default="pick_engine_v1")
    feature_set_version: Mapped[str] = mapped_column(String(40), default="fs1")
    consensus: Mapped[str] = mapped_column(String(30), default="model_only")
    recommendation_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"), nullable=True)
    user_decision: Mapped[str | None] = mapped_column(String(10), nullable=True)  # bet/pass
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_result: Mapped[str | None] = mapped_column(String(10), nullable=True)  # win/loss/push
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(3), nullable=True)  # A+..F
    include_in_metrics: Mapped[bool] = mapped_column(Boolean, default=True)


class Hypothesis(Base, TimestampMixin):
    __tablename__ = "hypotheses"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    test_type: Mapped[str] = mapped_column(String(40))   # see engines/research.py TEST_TYPES
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/archived
    last_result_json: Mapped[str] = mapped_column(Text, default="{}")
    prev_result_json: Mapped[str] = mapped_column(Text, default="{}")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trend: Mapped[str] = mapped_column(String(12), default="unknown")  # increasing/decreasing/flat


class PatternNote(Base):
    __tablename__ = "pattern_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(40))         # player_roi/odds_range/league/market/timing
    description: Mapped[str] = mapped_column(Text)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(12), default="proposed")  # proposed/approved/rejected


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    filters_json: Mapped[str] = mapped_column(Text, default="{}")  # backtester filter payload
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")


class MarketEvent(Base):
    """Market lifecycle: appear/disappear/odds_change/live_start/threshold_cross."""
    __tablename__ = "market_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True, index=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="")
    selection: Mapped[str] = mapped_column(String(30), default="")
    event_type: Mapped[str] = mapped_column(String(30))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class RawProviderResponse(Base):
    __tablename__ = "raw_provider_responses"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    provider: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(String(200))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="")
    # v0.3.5: which bookmaker a /v2/event/odds call was made for (None for
    # calls that don't take a source, and for rows written before this
    # column existed). Lets performance/health reporting break calls and
    # empty-response rate down per sportsbook.
    sportsbook: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # v0.3.7B: link back to the PollCycle that produced this response, and
    # (best-effort, may stay null for bulk/multi-event endpoints) the
    # specific provider event id this call was about.
    poll_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("poll_cycles.id"), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class PollCycle(Base):
    """v0.3.7B: one row per outbound provider HTTP call. True system-clock
    instrumentation for poll density, cadence, and quota tracking, separate
    from OddsSnapshot.collected_at (provider event-time -- see v0.3.7A
    Gate G1). Additive, new table; never touches historical rows."""
    __tablename__ = "poll_cycles"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40))
    intended_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poll_started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    response_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    request_duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    events_requested_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_rows_written_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload_empty: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    quota_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_reset_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class MarketAvailabilityRecord(Base):
    """v0.3.7B: heartbeat row written every poll cycle, even when odds are
    UNCHANGED. Pure change-only storage (the existing OddsSnapshot /
    MarketEvent 'disappeared' pattern) cannot distinguish 'we stopped
    polling' from 'the market genuinely vanished' -- this table exists
    specifically to close that gap so availability-state detection is
    possible going forward. See notes/triage/v0_3_7B-market-availability.md."""
    __tablename__ = "market_availability_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True, index=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="")
    selection: Mapped[str] = mapped_column(String(30), default="")
    poll_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("poll_cycles.id"), nullable=True)
    source_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    availability_state: Mapped[str] = mapped_column(String(40), default="UNKNOWN")
    odds_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    decimal_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds_to_kickoff: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class ExecutionClassification(Base):
    """v0.3.7B execution classifier v2: one primary state + coexisting
    diagnostic flags per PaperTrade row. Additive/report table -- never
    mutates PaperTrade.settlement_status. Historical rows (no system
    timestamps available) are always is_historical_degraded=True."""
    __tablename__ = "execution_classifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    paper_trade_id: Mapped[int] = mapped_column(ForeignKey("paper_trades.id"), index=True, unique=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    primary_state: Mapped[str] = mapped_column(String(40))
    diagnostic_flags_json: Mapped[str] = mapped_column(Text, default="[]")
    is_historical_degraded: Mapped[bool] = mapped_column(Boolean, default=True)
    # v0.3.7D: orthogonal to primary_state -- whether this signal could have
    # been a real pre-kickoff paper entry at all, independent of whether a
    # price was ever found for it. See engines/execution_classifier_v2.py.
    executability_label: Mapped[str | None] = mapped_column(String(30), nullable=True)


class ClosingRecord(Base):
    """v0.3.7B: one row per (match, book, market, selection) closing-price
    determination, going forward. Additive, new table. Never imputes a
    close; never devigs an incomplete 3-way market (all_three_outcomes_present
    must be True before any downstream devig is attempted)."""
    __tablename__ = "closing_records"
    __table_args__ = (UniqueConstraint("match_id", "sportsbook", "market", "selection"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="")
    selection: Mapped[str] = mapped_column(String(30), default="")
    close_source_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_ingested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_price_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_type: Mapped[str] = mapped_column(String(30), default="LAST_AVAILABLE")
    close_quality: Mapped[str] = mapped_column(String(10), default="INVALID")
    all_three_outcomes_present: Mapped[bool] = mapped_column(Boolean, default=False)
    updates_final_5m_count: Mapped[int] = mapped_column(Integer, default=0)
    updates_between_entry_and_close_count: Mapped[int] = mapped_column(Integer, default=0)
    market_available_at_close: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)



# ---------------------------------------------------------------------------
# v0.3.3 Prediction Lab — self-testing prediction loop
# ---------------------------------------------------------------------------

class PredictionLedger(Base):
    """Immutable frozen belief record.

    One row = what the platform believed at one time horizon for one market side.
    Never update prediction fields after creation. Scoring writes to
    PredictionScore instead.
    """
    __tablename__ = "prediction_ledger"
    __table_args__ = (UniqueConstraint("match_id", "horizon_label", "sportsbook", "market",
                                       "selection", "line", "model_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    horizon_label: Mapped[str] = mapped_column(String(20), index=True)  # T-30m/T-15m/...
    horizon_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prediction_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    model_version: Mapped[str] = mapped_column(String(60), index=True)
    feature_set_version: Mapped[str] = mapped_column(String(60), default="prediction_lab_fs1")
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="ML_3WAY")
    selection: Mapped[str] = mapped_column(String(30), default="")
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_decimal: Mapped[float] = mapped_column(Float, default=0.0)
    predicted_winner: Mapped[str] = mapped_column(String(8), default="")
    p_home: Mapped[float] = mapped_column(Float, default=0.0)
    p_draw: Mapped[float] = mapped_column(Float, default=0.0)
    p_away: Mapped[float] = mapped_column(Float, default=0.0)
    model_prob: Mapped[float] = mapped_column(Float, default=0.0)
    fair_decimal: Mapped[float] = mapped_column(Float, default=0.0)
    ev_pct: Mapped[float] = mapped_column(Float, default=0.0)
    predicted_first_live_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    predicted_first_live_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    steam_probability: Mapped[float] = mapped_column(Float, default=0.5)
    expected_line_movement_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    maximum_entry_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_entry_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_window: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(String(10), default="WAIT")  # BET/WAIT/PASS
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence_json: Mapped[str] = mapped_column(Text, default="{}")
    features_json: Mapped[str] = mapped_column(Text, default="{}")
    immutable_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="frozen")  # frozen/scored
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # v0.3.6: recommended execution mode, computed alongside the rest of the
    # frozen payload but NOT part of immutable_hash (hash payload is
    # unchanged from v0.3.4/v0.3.5 -- see engines/prediction_lab.py
    # _rebuild_freeze_payload). Purely additive, nullable, zero-migration.
    execution_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    execution_reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")


class PredictionReality(Base):
    """Reality observed after kickoff for one match/market/selection."""
    __tablename__ = "prediction_reality"
    __table_args__ = (UniqueConstraint("match_id", "sportsbook", "market", "selection", "line"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="ML_3WAY")
    selection: Mapped[str] = mapped_column(String(30), default="")
    line: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_pre_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_live_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_pre_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_pre_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_live_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_live_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_live_after_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_movement_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_shortened: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    winner: Mapped[str | None] = mapped_column(String(8), nullable=True)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_quality_score: Mapped[int] = mapped_column(Integer, default=0)
    dataset_tier: Mapped[str] = mapped_column(String(12), default="rejected")  # gold/silver/rejected
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PredictionScore(Base):
    """Score for a frozen PredictionLedger row."""
    __tablename__ = "prediction_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("prediction_ledger.id"), unique=True, index=True)
    reality_id: Mapped[int] = mapped_column(ForeignKey("prediction_reality.id"), index=True)
    winner_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    steam_direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    magnitude_error_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_window_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_bucket: Mapped[str] = mapped_column(String(40), default="UNKNOWN")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# v0.3.6 — Profit Validation Layer
# Friend Pick Ledger, Paper Trade Engine, Book Coverage Scanner, Feed
# Shootout prep. See docs/STABILIZATION_CHANGELOG_v0.3.6.md.
# ---------------------------------------------------------------------------

class FriendPick(Base):
    """A friend's pick, treated as a timestamped signal source -- not truth.

    Immutable after creation: corrections are new rows via corrects_pick_id,
    never edits to the original. effective_known_at is the leakage anchor --
    every scoring/odds lookup for this pick uses effective_known_at, never
    pick_timestamp, so a backfilled pick can never be scored as if it were
    known earlier than the moment it actually entered the system."""
    __tablename__ = "friend_picks"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pick_timestamp: Mapped[datetime] = mapped_column(DateTime)
    effective_known_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    is_backfilled: Mapped[bool] = mapped_column(Boolean, default=False)
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    league: Mapped[str] = mapped_column(String(120), default="")
    home_name: Mapped[str] = mapped_column(String(160), default="")
    away_name: Mapped[str] = mapped_column(String(160), default="")
    kickoff_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pick_side: Mapped[str] = mapped_column(String(8))  # home/away/draw
    odds_at_pick_american: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odds_at_pick_decimal: Mapped[float] = mapped_column(Float)
    book_seen: Mapped[str] = mapped_column(String(80), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)  # high/medium/low
    resolution_status: Mapped[str] = mapped_column(String(12), default="PENDING")  # RESOLVED/PENDING/UNRESOLVED
    scoring_status: Mapped[str] = mapped_column(String(10), default="pending")  # pending/scored
    corrects_pick_id: Mapped[int | None] = mapped_column(ForeignKey("friend_picks.id"), nullable=True)
    immutable_hash: Mapped[str] = mapped_column(String(64), index=True)
    execution_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    execution_reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")


class FriendPickScore(Base):
    """Score for a RESOLVED FriendPick, computed once reality/result exists."""
    __tablename__ = "friend_pick_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    friend_pick_id: Mapped[int] = mapped_column(ForeignKey("friend_picks.id"), unique=True, index=True)
    winner_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    steam_direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # No friend-stated expected movement exists to grade magnitude against --
    # left null always; post_pick_movement_cents is the honest observed data
    # point instead of a fabricated "error vs claim".
    first_live_movement_error_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_pick_movement_cents: Mapped[float | None] = mapped_column(Float, nullable=True)
    proxy_clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_price_survived: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    paper_stake: Mapped[float] = mapped_column(Float, default=1.0)
    paper_pl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    vs_model_comparison: Mapped[str] = mapped_column(String(20), default="NOT_AVAILABLE")  # BEAT/LOST/TIE/NOT_AVAILABLE
    vs_baseline_comparison: Mapped[str] = mapped_column(String(20), default="NOT_AVAILABLE")
    error_bucket: Mapped[str] = mapped_column(String(40), default="DATA_UNAVAILABLE")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaperTrade(Base):
    """Simulated (never real) entry for a model or friend signal at a given
    delay after the signal became known. Reference-feed prices only -- see
    feed_lag_caveat. Never fabricates a fill: MISSED_PRICE when no snapshot
    exists within 60s of the target time."""
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    signal_source: Mapped[str] = mapped_column(String(10), index=True)  # MODEL/FRIEND
    signal_id: Mapped[int] = mapped_column(Integer, index=True)  # PredictionLedger.id or FriendPick.id
    match_id: Mapped[int | None] = mapped_column(ForeignKey("matches.id"), nullable=True)
    sportsbook: Mapped[str] = mapped_column(String(60), default="")
    market: Mapped[str] = mapped_column(String(30), default="ML_3WAY")
    selection: Mapped[str] = mapped_column(String(30), default="")
    signal_time: Mapped[datetime] = mapped_column(DateTime)
    delay_seconds: Mapped[int] = mapped_column(Integer)
    price_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_entry_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_survived: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    paper_stake: Mapped[float] = mapped_column(Float, default=1.0)
    paper_pl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    proxy_clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # SIGNAL_CREATED/ATTEMPTED/FILLED/MISSED_PRICE/VOID/SETTLED
    settlement_status: Mapped[str] = mapped_column(String(20), default="SIGNAL_CREATED")
    book_availability: Mapped[str | None] = mapped_column(String(20), nullable=True)
    feed_lag_caveat: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("signal_source", "signal_id", "delay_seconds"),)


class BookmakerCoverage(Base):
    """One row per source, upserted on each scan. Never touched by the hot
    poller -- see engines/book_coverage.py. bet365 stays the permanent
    reference feed and is never flagged execution_candidate."""
    __tablename__ = "bookmaker_coverage"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    events_queried: Mapped[int] = mapped_column(Integer, default=0)
    non_empty_responses: Mapped[int] = mapped_column(Integer, default=0)
    empty_responses: Mapped[int] = mapped_column(Integer, default=0)
    error_responses: Mapped[int] = mapped_column(Integer, default=0)
    leagues_seen_json: Mapped[str] = mapped_column(Text, default="[]")
    markets_seen_json: Mapped[str] = mapped_column(Text, default="[]")
    ml_3way_available: Mapped[bool] = mapped_column(Boolean, default=False)
    spread_2way_available: Mapped[bool] = mapped_column(Boolean, default=False)
    live_odds_available: Mapped[bool] = mapped_column(Boolean, default=False)
    response_latency_ms_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_live_availability: Mapped[str] = mapped_column(String(12), default="UNKNOWN")
    last_successful_observation: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="UNKNOWN")  # WORKS/EMPTY/BROKEN/UNKNOWN
    execution_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")


class FeedCandidate(Base):
    """Provider comparison framework (v0.3.6 Module 4) -- schema + manual
    notes only. No paid integrations wired in this version."""
    __tablename__ = "feed_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(60), unique=True)
    supported_leagues_json: Mapped[str] = mapped_column(Text, default="[]")
    supported_markets_json: Mapped[str] = mapped_column(Text, default="[]")
    supported_books_json: Mapped[str] = mapped_column(Text, default="[]")
    first_live_latency_note: Mapped[str] = mapped_column(Text, default="")
    timestamp_quality: Mapped[str] = mapped_column(String(80), default="")
    raw_payload_availability: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="UNKNOWN")  # CANDIDATE/TESTING/WORKS/FAILED/UNKNOWN
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
