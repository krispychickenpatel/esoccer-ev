"""v0.3.7D.1 Task 11/12: spot-check readiness is coverage evidence, never a
pass/fail gate -- zero spot-checks must not be read as failed execution,
and the SIMULATED_FILLED label must flip only once coverage is sufficient."""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import spot_check_readiness
from app.models import Base, Match, PaperTrade, Player, PredictionLedger, Settings


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _trade(db, match_id, status="FILLED"):
    row = PaperTrade(match_id=match_id, signal_id=1, signal_source="MODEL", delay_seconds=30,
                     selection="home", settlement_status=status, created_at=datetime(2026, 1, 1),
                     signal_time=datetime(2026, 1, 1), market="ML_3WAY", sportsbook="bet365")
    db.add(row); db.commit()
    return row


def test_zero_spot_checks_is_not_validated_but_not_a_failure(monkeypatch):
    db = _db()
    h = Player(name="H", league="L"); a = Player(name="A", league="L")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=datetime(2026, 1, 1), league="L", home_player_id=h.id, away_player_id=a.id,
             source="betsapi", verification_status="api_verified")
    db.add(m); db.flush()
    _trade(db, m.id)

    monkeypatch.setattr(spot_check_readiness, "SPOT_CHECK_CSV", __import__("pathlib").Path("/nonexistent/x.csv"))
    r = spot_check_readiness.spot_check_readiness_report(db)
    assert r["simulated_filled_count"] == 1
    assert r["spot_check_count"] == 0
    assert r["sufficient_for_validated_label"] is False
    assert r["label"] == spot_check_readiness.SIMULATED_FILLED_LABEL_INSUFFICIENT
    # coverage evidence, not proof of failure
    assert "not that execution failed" in r["note"]


def test_sufficient_spot_checks_flip_label(monkeypatch, tmp_path):
    import csv
    csv_path = tmp_path / "spot_checks.csv"
    fields = ["book", "displayed_price", "provider_latest_price", "market_available_on_book",
             "market_available_on_provider"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i in range(spot_check_readiness.SUFFICIENT_SPOT_CHECK_N):
            w.writerow({"book": "FanDuel", "displayed_price": "2.10", "provider_latest_price": "2.10",
                       "market_available_on_book": "true", "market_available_on_provider": "true"})

    monkeypatch.setattr(spot_check_readiness, "SPOT_CHECK_CSV", csv_path)
    db = _db()
    r = spot_check_readiness.spot_check_readiness_report(db)
    assert r["spot_check_count"] == spot_check_readiness.SUFFICIENT_SPOT_CHECK_N
    assert r["sufficient_for_validated_label"] is True
    assert r["label"] == spot_check_readiness.SIMULATED_FILLED_LABEL_SUFFICIENT
    assert r["books_checked"] == ["FanDuel"]
    assert r["price_within_tolerance_n"] == spot_check_readiness.SUFFICIENT_SPOT_CHECK_N
    assert r["market_availability_match_n"] == spot_check_readiness.SUFFICIENT_SPOT_CHECK_N
    assert r["market_availability_mismatch_n"] == 0


def test_price_outside_tolerance_and_availability_mismatch_detected(monkeypatch, tmp_path):
    import csv
    csv_path = tmp_path / "spot_checks.csv"
    fields = ["book", "displayed_price", "provider_latest_price", "market_available_on_book",
             "market_available_on_provider"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerow({"book": "FanDuel", "displayed_price": "2.50", "provider_latest_price": "2.10",
                   "market_available_on_book": "true", "market_available_on_provider": "false"})

    monkeypatch.setattr(spot_check_readiness, "SPOT_CHECK_CSV", csv_path)
    db = _db()
    r = spot_check_readiness.spot_check_readiness_report(db)
    assert r["price_within_tolerance_n"] == 0
    assert r["price_tolerance_evaluable_n"] == 1
    assert r["market_availability_mismatch_n"] == 1
    assert r["market_availability_match_n"] == 0
