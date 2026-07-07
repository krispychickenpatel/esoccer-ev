"""v0.3.7B Section 4: execution classification v2."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engines import execution_classifier_v2 as ecv2
from app.engines import odds_math, paper_trade
from app.models import Base, ExecutionClassification, Match, OddsSnapshot, PaperTrade, Player, \
    PredictionLedger, Settings


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
             home_player_id=h.id, away_player_id=a.id, source="betsapi",
             verification_status="api_verified")
    db.add(m); db.flush()
    return m


def _pred(db, match, selection="home", prediction_time=None, current_decimal=2.2,
          max_entry=None, predicted_winner="home"):
    pt = prediction_time or (match.start_time - timedelta(minutes=5))
    row = PredictionLedger(
        match_id=match.id, horizon_label="T-5m", prediction_time=pt,
        scheduled_start=match.start_time, model_version="v", sportsbook="bet365",
        market="ML_3WAY", selection=selection, current_decimal=current_decimal,
        predicted_winner=predicted_winner, model_prob=0.5,
        maximum_entry_decimal=max_entry if max_entry is not None else current_decimal,
        action="WAIT", status="scored",
        immutable_hash=f"h-{match.id}-{selection}-{pt.isoformat()}")
    db.add(row); db.commit()
    return row


def test_no_data_at_entry_is_primary_when_no_snapshot_exists():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    pred = _pred(db, m)
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    primary, flags, degraded = ecv2.classify_paper_trade(db, trade)
    assert primary == ecv2.NO_DATA_AT_ENTRY
    assert degraded is True  # no OddsSnapshot at all behind this trade


def test_price_below_entry_floor_not_moved_past_max_entry_wording():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    pred = _pred(db, m, current_decimal=2.2, max_entry=2.2)
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=90, decimal_odds=2.1, implied_prob=round(1/2.1, 4),
                        collected_at=start - timedelta(minutes=5), phase="pre_match",
                        data_source="betsapi", verification_status="api_verified",
                        polled_at=start - timedelta(minutes=5), ingested_at=start - timedelta(minutes=5),
                        response_received_at=start - timedelta(minutes=5)))
    db.commit()
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    primary, flags, degraded = ecv2.classify_paper_trade(db, trade)
    assert primary == ecv2.PRICE_BELOW_ENTRY_FLOOR
    assert primary != "MOVED_PAST_MAX_ENTRY"
    assert "MOVED_PAST_MAX_ENTRY" not in dir(ecv2)


def test_primary_state_and_diagnostic_flags_coexist():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # floor == signal price (the real-world v0.3.7A anomaly) + a fresh snapshot.
    pred = _pred(db, m, current_decimal=2.2, max_entry=2.2)
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.2, implied_prob=round(1/2.2, 4),
                        collected_at=start - timedelta(minutes=5), phase="pre_match",
                        data_source="betsapi", verification_status="api_verified",
                        polled_at=start - timedelta(minutes=5), ingested_at=start - timedelta(minutes=5),
                        response_received_at=start - timedelta(minutes=5)))
    db.commit()
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    primary, flags, degraded = ecv2.classify_paper_trade(db, trade)
    assert primary in (ecv2.FILLED,)
    assert "floor_equals_signal_price" in flags
    assert "no_discount_applied" in flags


def test_historical_row_flagged_degraded_and_stored_without_mutating_paper_trade():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    pred = _pred(db, m, current_decimal=2.2, max_entry=2.2)
    # Historical snapshot: no system timestamps at all.
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.2, implied_prob=round(1/2.2, 4),
                        collected_at=start - timedelta(minutes=5), phase="pre_match",
                        data_source="betsapi", verification_status="api_verified"))
    db.commit()
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    original_status = trade.settlement_status
    row = ecv2.classify_and_store(db, trade)
    assert row.is_historical_degraded is True
    assert "provider_time_only_historical_row" in row.diagnostic_flags_json
    db.refresh(trade)
    assert trade.settlement_status == original_status  # never mutated


def test_classify_all_shows_no_data_at_entry_as_dominant_state():
    """Reproduces the v0.3.7A finding: with no odds history, NO_DATA_AT_ENTRY
    must dominate, not PRICE_BELOW_ENTRY_FLOOR."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        m = _match(db, start + timedelta(minutes=i), home=f"H{i}", away=f"A{i}")
        _pred(db, m)
    for pred in db.scalars(select(PredictionLedger)).all():
        paper_trade.simulate_model_candidate(db, pred.id)
    result = ecv2.classify_all(db)
    assert result["by_primary_state"].get(ecv2.NO_DATA_AT_ENTRY, 0) > \
        result["by_primary_state"].get(ecv2.PRICE_BELOW_ENTRY_FLOOR, 0)
    assert result["historical_degraded_count"] == result["total_classified"]  # no system ts anywhere
