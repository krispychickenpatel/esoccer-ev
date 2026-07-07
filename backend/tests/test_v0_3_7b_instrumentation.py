"""v0.3.7B Section 1-3: timestamp instrumentation, densified poll scheduler,
market availability heartbeat/candidate detection."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engines import market_availability, poll_scheduler
from app.models import Base, Match, MarketAvailabilityRecord, OddsSnapshot, Player, Settings
from app.services import poller


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _match(db, start, home="H", away="A"):
    h = Player(name=home, league="Esoccer Battle - 8 mins play")
    a = Player(name=away, league="Esoccer Battle - 8 mins play")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league="Esoccer Battle - 8 mins play",
             home_player_id=h.id, away_player_id=a.id, ext_id="ext-1",
             source="betsapi", verification_status="api_verified")
    db.add(m); db.flush()
    return m


# --------------------------------------------------- 1: timestamp ordering

def test_new_odds_rows_get_source_ts_polled_at_response_received_at_ingested_at():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    t0 = start - timedelta(minutes=5)
    incoming = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "home",
        "line": None, "decimal_odds": 2.0, "american_odds": 100, "implied_prob": 0.5,
        "collected_at": t0, "is_opening": False, "is_closing": False,
        "source_ts": t0, "polled_at": t0 - timedelta(milliseconds=50),
        "response_received_at": t0 - timedelta(milliseconds=10),
        "poll_cycle_id": None, "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    poller.process_snapshots(db, m, incoming, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    row = db.scalar(select(OddsSnapshot))
    assert row.source_ts == t0
    assert row.polled_at is not None
    assert row.response_received_at is not None
    assert row.ingested_at is not None
    assert row.ingested_at >= row.response_received_at >= row.polled_at


def test_historical_rows_with_null_system_timestamps_handled_safely():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Simulates a pre-v0.3.7B row: no polled_at/response_received_at/ingested_at.
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.0, implied_prob=0.5,
                        collected_at=start - timedelta(minutes=5), phase="pre_match",
                        data_source="betsapi", verification_status="api_verified"))
    db.commit()
    row = db.scalar(select(OddsSnapshot))
    assert row.polled_at is None
    assert row.response_received_at is None
    assert row.ingested_at is None
    assert row.source_ts is None  # provider-time vs system-time not conflated


def test_provider_time_and_system_time_are_distinct_fields():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    provider_time = start - timedelta(minutes=5)
    our_poll_time = provider_time + timedelta(seconds=25)  # simulated feed lag
    incoming = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "home",
        "line": None, "decimal_odds": 2.0, "american_odds": 100, "implied_prob": 0.5,
        "collected_at": provider_time, "is_opening": False, "is_closing": False,
        "source_ts": provider_time, "polled_at": our_poll_time,
        "response_received_at": our_poll_time, "poll_cycle_id": None,
        "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    poller.process_snapshots(db, m, incoming, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    row = db.scalar(select(OddsSnapshot))
    assert row.collected_at == provider_time
    assert row.polled_at == our_poll_time
    assert row.polled_at != row.collected_at


# --------------------------------------------------- 2: densified scheduler

def test_densified_window_activation():
    assert poll_scheduler.in_densified_window(300) is True   # T-5min
    assert poll_scheduler.in_densified_window(-60) is True   # live+1min
    assert poll_scheduler.in_densified_window(700) is False  # outside, too early
    assert poll_scheduler.in_densified_window(-200) is False  # outside, too late


def test_quota_guard_degrades_cadence_rather_than_failing():
    cap = 60.0
    light = poll_scheduler.densified_cadence_seconds(pressure_pct=10.0, quota_pct_cap=cap)
    medium = poll_scheduler.densified_cadence_seconds(pressure_pct=35.0, quota_pct_cap=cap)
    heavy = poll_scheduler.densified_cadence_seconds(pressure_pct=59.0, quota_pct_cap=cap)
    assert light == 10.0
    assert medium == 15.0
    assert heavy == 30.0
    assert poll_scheduler.quota_budget_ok(calls_last_hour=100, hourly_quota_cap=3600, quota_pct_cap=60.0)
    assert not poll_scheduler.quota_budget_ok(calls_last_hour=2200, hourly_quota_cap=3600, quota_pct_cap=60.0)


def test_429_triggers_backoff_circuit_breaker():
    cb = poll_scheduler.CircuitBreakerState(failure_threshold=3, base_cooldown_s=10.0)
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert cb.is_open(now) is False
    cb.record_failure(now)
    cb.record_failure(now)
    assert cb.is_open(now) is False  # not tripped yet (below threshold)
    cb.record_failure(now)
    assert cb.is_open(now) is True  # tripped on 3rd consecutive failure
    # still open before cooldown elapses
    assert cb.is_open(now + timedelta(seconds=5)) is True
    # cooldown elapsed -> half-open (closed until next failure)
    assert cb.is_open(now + timedelta(seconds=11)) is False


def test_circuit_breaker_success_resets_failure_count():
    cb = poll_scheduler.CircuitBreakerState(failure_threshold=3)
    now = datetime(2026, 1, 1, 12, 0, 0)
    cb.record_failure(now)
    cb.record_failure(now)
    cb.record_success()
    assert cb.consecutive_failures == 0
    cb.record_failure(now)
    cb.record_failure(now)
    assert cb.is_open(now) is False  # reset means we're back to 2, not tripped


# --------------------------------------------------- heartbeat/availability

def test_heartbeat_records_created_when_odds_unchanged():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    incoming = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "home",
        "line": None, "decimal_odds": 2.0, "american_odds": 100, "implied_prob": 0.5,
        "collected_at": start - timedelta(minutes=5), "is_opening": False, "is_closing": False,
        "source_ts": start - timedelta(minutes=5), "polled_at": start - timedelta(minutes=5),
        "response_received_at": start - timedelta(minutes=5), "poll_cycle_id": None,
        "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    # First call writes the snapshot + heartbeats.
    poller.process_snapshots(db, m, incoming, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    n1 = len(db.scalars(select(MarketAvailabilityRecord)).all())
    assert n1 == 3  # home/draw/away heartbeats even though only home priced
    # Second call, SAME odds (no change) -- must still write new heartbeats.
    poller.process_snapshots(db, m, incoming, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    n2 = len(db.scalars(select(MarketAvailabilityRecord)).all())
    assert n2 == 6  # heartbeats written again even with unchanged odds


def test_heartbeat_written_even_when_incoming_is_empty():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    poller.process_snapshots(db, m, [], sportsbook="bet365", tracked_markets=["ML_3WAY"])
    rows = db.scalars(select(MarketAvailabilityRecord)).all()
    assert len(rows) == 3
    assert all(r.availability_state == market_availability.EMPTY_PROVIDER_RESPONSE for r in rows)


def test_market_availability_state_transitions_present_absent():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    present_tick = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "home",
        "line": None, "decimal_odds": 2.0, "american_odds": 100, "implied_prob": 0.5,
        "collected_at": start - timedelta(minutes=9), "is_opening": False, "is_closing": False,
        "source_ts": start - timedelta(minutes=9), "polled_at": start - timedelta(minutes=9),
        "response_received_at": start - timedelta(minutes=9), "poll_cycle_id": None,
        "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    poller.process_snapshots(db, m, present_tick, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    home_state_1 = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.selection == "home").order_by(MarketAvailabilityRecord.id.desc()))
    assert home_state_1.availability_state == market_availability.PRESENT

    # Next poll: home no longer in the payload (draw/away still there) -> ABSENT.
    absent_tick = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "draw",
        "line": None, "decimal_odds": 3.0, "american_odds": 200, "implied_prob": 0.33,
        "collected_at": start - timedelta(minutes=8), "is_opening": False, "is_closing": False,
        "source_ts": start - timedelta(minutes=8), "polled_at": start - timedelta(minutes=8),
        "response_received_at": start - timedelta(minutes=8), "poll_cycle_id": None,
        "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    poller.process_snapshots(db, m, absent_tick, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    home_state_2 = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.selection == "home").order_by(MarketAvailabilityRecord.id.desc()))
    assert home_state_2.availability_state == market_availability.ABSENT


def test_market_withdrawn_prekickoff_candidate_detection():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Present well before the window, then absent throughout the bounded
    # pre-kickoff window, never present again before start.
    db.add(MarketAvailabilityRecord(observed_at=start - timedelta(minutes=20), match_id=m.id,
                                    sportsbook="bet365", market="ML_3WAY", selection="home",
                                    availability_state=market_availability.PRESENT))
    for mins in (9, 7, 5, 3, 1):
        db.add(MarketAvailabilityRecord(observed_at=start - timedelta(minutes=mins), match_id=m.id,
                                        sportsbook="bet365", market="ML_3WAY", selection="home",
                                        availability_state=market_availability.ABSENT))
    db.commit()
    result = market_availability.detect_withdrawal_relist_candidates(db, m, "bet365", "ML_3WAY", "home")
    assert result["withdrawn_candidate"] is True
    assert result["relisted_candidate"] is False


def test_relisted_live_at_kickoff_candidate_detection():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add(MarketAvailabilityRecord(observed_at=start - timedelta(minutes=20), match_id=m.id,
                                    sportsbook="bet365", market="ML_3WAY", selection="home",
                                    availability_state=market_availability.PRESENT))
    for mins in (9, 7, 5, 3, 1):
        db.add(MarketAvailabilityRecord(observed_at=start - timedelta(minutes=mins), match_id=m.id,
                                        sportsbook="bet365", market="ML_3WAY", selection="home",
                                        availability_state=market_availability.ABSENT))
    db.add(MarketAvailabilityRecord(observed_at=start + timedelta(seconds=5), match_id=m.id,
                                    sportsbook="bet365", market="ML_3WAY", selection="home",
                                    availability_state=market_availability.PRESENT))
    db.commit()
    result = market_availability.detect_withdrawal_relist_candidates(db, m, "bet365", "ML_3WAY", "home")
    assert result["withdrawn_candidate"] is True
    assert result["relisted_candidate"] is True


def test_prevalence_report_does_not_claim_borderline_with_zero_data():
    db = _db()
    result = market_availability.prevalence_report(db)
    assert result["total_match_book_market_selection_combos_checked"] == 0
    assert result["withdrawn_prevalence_pct"] is None
    assert "NO HEARTBEAT DATA YET" in result["recommendation"]


def test_no_provider_time_metric_mislabeled_as_system_time():
    """collected_at must never be silently copied into a *_at system-time
    field name other than source_ts (its explicit documented alias)."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    provider_time = start - timedelta(minutes=5)
    incoming = [{
        "ext_id": "ext-1", "sportsbook": "bet365", "market": "ML_3WAY", "selection": "home",
        "line": None, "decimal_odds": 2.0, "american_odds": 100, "implied_prob": 0.5,
        "collected_at": provider_time, "is_opening": False, "is_closing": False,
        # polled_at/response_received_at deliberately omitted (simulates a
        # caller that didn't populate them) -- must stay None, not silently
        # fall back to provider time.
        "poll_cycle_id": None, "provider_event_id": "ext-1", "provider_book": "bet365",
    }]
    poller.process_snapshots(db, m, incoming, sportsbook="bet365", tracked_markets=["ML_3WAY"])
    row = db.scalar(select(OddsSnapshot))
    assert row.polled_at is None
    assert row.response_received_at is None
    assert row.source_ts == provider_time  # only source_ts aliases collected_at
