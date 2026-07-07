"""v0.3.7B Sections 5-7: entry floor diagnostics, closing records, CLV
forward readiness."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engines import clv_forward_readiness, closing_records, entry_floor_diagnostics
from app.engines.market_availability import PRESENT
from app.models import (Base, ClosingRecord, Match, MarketAvailabilityRecord, OddsSnapshot,
                        Player, PredictionLedger, Settings)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _match(db, start, home="H", away="A", scored=True):
    h = Player(name=home, league="Esoccer Battle - 8 mins play")
    a = Player(name=away, league="Esoccer Battle - 8 mins play")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league="Esoccer Battle - 8 mins play",
             home_player_id=h.id, away_player_id=a.id, source="betsapi",
             verification_status="api_verified")
    if scored:
        m.home_score, m.away_score, m.winner = 1, 0, "home"
    db.add(m); db.flush()
    return m


def _snap(match_id, book, selection, dec, at, phase="pre_match", with_system_ts=False):
    kwargs = dict(match_id=match_id, sportsbook=book, market="ML_3WAY", selection=selection,
                 american_odds=100, decimal_odds=dec, implied_prob=round(1 / dec, 4),
                 collected_at=at, phase=phase, data_source="betsapi",
                 verification_status="api_verified")
    if with_system_ts:
        kwargs.update(polled_at=at, response_received_at=at, ingested_at=at)
    return OddsSnapshot(**kwargs)


def _pred(db, match, selection="home", current_decimal=2.2, max_entry=None, steam_prob=0.3):
    pt = match.start_time - timedelta(minutes=5)
    row = PredictionLedger(
        match_id=match.id, horizon_label="T-5m", prediction_time=pt,
        scheduled_start=match.start_time, model_version="v", sportsbook="bet365",
        market="ML_3WAY", selection=selection, current_decimal=current_decimal,
        predicted_winner="home", model_prob=0.5,
        maximum_entry_decimal=max_entry if max_entry is not None else current_decimal,
        steam_probability=steam_prob, action="WAIT", status="scored",
        immutable_hash=f"h-{match.id}-{selection}-{pt.isoformat()}")
    db.add(row); db.commit()
    return row


# --------------------------------------------------- 5: entry floor diagnostics

def test_entry_floor_diagnostics_counts_floor_equals_signal_price():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m1 = _match(db, start, "H1", "A1")
    m2 = _match(db, start + timedelta(minutes=10), "H2", "A2")
    _pred(db, m1, current_decimal=2.0, max_entry=2.0, steam_prob=0.3)  # equal
    _pred(db, m2, current_decimal=2.0, max_entry=1.8, steam_prob=0.6)  # discount applied
    result = entry_floor_diagnostics.run(db)
    assert result["floor_equals_signal_price_count"] == 1
    assert result["floor_below_signal_price_count"] == 1
    assert result["steam_probability_distribution"]["n"] == 2
    assert result["steam_probability_distribution"]["count_gte_055_trigger"] == 1
    assert "discount_1pct" in result["whatif_lower_floor_simulation"]
    assert result["analysis_only_disclaimer"]  # must be present and non-empty


# --------------------------------------------------- 6: closing records

def test_closing_record_never_imputes_when_no_snapshot_exists():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    row = closing_records.build_closing_record(db, m, "bet365", "ML_3WAY", "home")
    assert row is None


def test_closing_record_high_quality_requires_all_three_and_freshness_and_availability():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    at = start - timedelta(seconds=30)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.0, at - timedelta(minutes=4), with_system_ts=True),
        _snap(m.id, "bet365", "home", 2.0, at, with_system_ts=True),
        _snap(m.id, "bet365", "draw", 3.2, at, with_system_ts=True),
        _snap(m.id, "bet365", "away", 3.5, at, with_system_ts=True),
    ])
    db.add(MarketAvailabilityRecord(observed_at=at, match_id=m.id, sportsbook="bet365",
                                    market="ML_3WAY", selection="home",
                                    availability_state=PRESENT))
    db.commit()
    row = closing_records.build_closing_record(db, m, "bet365", "ML_3WAY", "home")
    assert row is not None
    assert row.all_three_outcomes_present is True
    assert row.close_quality == closing_records.HIGH
    assert row.close_type in (closing_records.PRE_KICKOFF,)


def test_historical_closing_record_capped_below_high_when_no_system_timestamps():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    at = start - timedelta(seconds=30)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.0, at, with_system_ts=False),
        _snap(m.id, "bet365", "draw", 3.2, at, with_system_ts=False),
        _snap(m.id, "bet365", "away", 3.5, at, with_system_ts=False),
    ])
    db.commit()
    row = closing_records.build_closing_record(db, m, "bet365", "ML_3WAY", "home")
    assert row is not None
    assert row.close_quality != closing_records.HIGH
    assert "DEGRADED_NO_SYSTEM_TIMESTAMPS" in row.flags_json


def test_incomplete_three_way_market_never_silently_devigged_all_three_false():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    at = start - timedelta(seconds=30)
    # Only home priced -- draw/away missing.
    db.add(_snap(m.id, "bet365", "home", 2.0, at, with_system_ts=True))
    db.commit()
    row = closing_records.build_closing_record(db, m, "bet365", "ML_3WAY", "home")
    assert row.all_three_outcomes_present is False
    assert row.close_quality != closing_records.HIGH


# --------------------------------------------------- 7: CLV forward readiness

def test_clv_refuses_decision_grade_verdict_below_n_150():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        m = _match(db, start + timedelta(minutes=i), f"H{i}", f"A{i}")
        _pred(db, m, current_decimal=2.0)
        at = m.start_time - timedelta(seconds=30)
        db.add_all([
            _snap(m.id, "bet365", "home", 2.0, at, with_system_ts=False),
            _snap(m.id, "bet365", "draw", 3.2, at, with_system_ts=False),
            _snap(m.id, "bet365", "away", 3.5, at, with_system_ts=False),
        ])
        db.commit()
        closing_records.build_closing_record(db, m, "bet365", "ML_3WAY", "home")
    report = clv_forward_readiness.historical_clv_report(db)
    assert report["status"] == "DEGRADED"
    assert report["sample_grade"] != "DECISION-GRADE (by sample size only -- still DEGRADED, provider-time)"
    assert report["distinct_samples_with_close"] < 150


def test_forward_clv_reports_pending_with_zero_system_timestamped_samples():
    db = _db()
    report = clv_forward_readiness.forward_clv_readiness(db)
    assert report["forward_system_timestamped_samples"] == 0
    assert "NOT READY" in report["readiness_verdict"]
