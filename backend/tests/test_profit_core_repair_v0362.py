"""v0.3.6.2 Profit Core Repair: model paper-trade eligibility fix +
Winner Edge Truth Layer."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engines import odds_math, paper_trade, profit_gates, winner_edge
from app.models import (Base, Match, OddsSnapshot, PaperTrade, Player,
                        PredictionLedger, Settings)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1, min_ev_pct=1.0))
    db.commit()
    return db


def _match(db, start, home="H", away="A", league="Esoccer Battle - 8 mins play"):
    h = Player(name=home, league=league)
    a = Player(name=away, league=league)
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league=league, home_player_id=h.id, away_player_id=a.id,
             source="betsapi", verification_status="api_verified")
    db.add(m); db.flush()
    return m


def _snap(match_id, book, selection, dec, at, s2k, phase="pre_match"):
    return OddsSnapshot(
        match_id=match_id, sportsbook=book, market="ML_3WAY", selection=selection,
        line=None, american_odds=odds_math.decimal_to_american(dec), decimal_odds=dec,
        implied_prob=odds_math.implied_prob(dec), collected_at=at, seconds_to_kickoff=s2k,
        phase=phase, data_source="betsapi", verification_status="api_verified")


def _legacy_prediction(db, match, selection="home", action="WAIT", execution_mode=None,
                       prediction_time=None, current_decimal=2.2, max_entry=None,
                       predicted_winner="home", model_prob=0.5, status="scored"):
    """Simulates a pre-v0.3.6 (or validation-mode) frozen prediction:
    execution_mode is None, action is never 'BET' -- exactly what real
    production data looks like."""
    pt = prediction_time or (match.start_time - timedelta(minutes=5))
    row = PredictionLedger(
        match_id=match.id, horizon_label="T-5m", prediction_time=pt,
        scheduled_start=match.start_time, model_version="v", sportsbook="bet365",
        market="ML_3WAY", selection=selection, current_decimal=current_decimal,
        predicted_winner=predicted_winner, model_prob=model_prob,
        maximum_entry_decimal=max_entry, action=action, execution_mode=execution_mode,
        status=status, immutable_hash=f"h-{match.id}-{selection}-{pt.isoformat()}")
    db.add(row); db.commit()
    return row


# --------------------------------------------------- 1: legacy eligibility

def test_legacy_prediction_with_null_execution_mode_is_paper_trade_eligible():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    pred = _legacy_prediction(db, m, action="WAIT", execution_mode=None)
    assert pred.action != "BET"
    assert pred.execution_mode is None

    trades = paper_trade.simulate_model_candidate(db, pred.id)
    assert trades is not None
    assert len(trades) == len(paper_trade.DELAYS_SECONDS)


# --------------------------------------------------- 2/3: simulate-all + dedup

def test_simulate_all_creates_model_paper_trades_from_eligible_predictions():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m1 = _match(db, start, home="H1", away="A1")
    m2 = _match(db, start + timedelta(minutes=10), home="H2", away="A2")
    _legacy_prediction(db, m1, action="PASS")
    _legacy_prediction(db, m2, action="WAIT")

    result = paper_trade.simulate_all(db)
    assert result["model"]["eligible_signals"] == 2
    assert result["model"]["created_trades"] == 2 * len(paper_trade.DELAYS_SECONDS)
    assert result["model_signals_simulated"] == 2
    total = len(db.scalars(select(PaperTrade)).all())
    assert total == 2 * len(paper_trade.DELAYS_SECONDS)


def test_running_simulate_all_twice_does_not_duplicate_rows():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    _legacy_prediction(db, m, action="PASS")

    r1 = paper_trade.simulate_all(db)
    assert r1["model"]["created_trades"] == len(paper_trade.DELAYS_SECONDS)
    r2 = paper_trade.simulate_all(db)
    assert r2["model"]["created_trades"] == 0
    assert r2["model"]["existing_trades"] == len(paper_trade.DELAYS_SECONDS)

    total = len(db.scalars(select(PaperTrade)).all())
    assert total == len(paper_trade.DELAYS_SECONDS)  # never duplicated


# --------------------------------------------------- 4: missing price honesty

def test_missing_price_creates_missed_price_never_fabricated_fill():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # No OddsSnapshot rows exist at all.
    pred = _legacy_prediction(db, m, action="WAIT")
    trades = paper_trade.simulate_model_candidate(db, pred.id)
    for t in trades:
        assert t.settlement_status == "MISSED_PRICE"
        assert t.price_decimal is None
        assert t.entry_survived is False
        assert t.paper_pl_usd is None


# --------------------------------------------------- 5: eligibility skip reasons

def test_eligibility_endpoint_reports_skip_reasons():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    _legacy_prediction(db, m, action="WAIT")
    report = paper_trade.eligibility_report(db)
    assert report["model"]["prediction_ledger_total"] == 1
    assert report["model"]["eligible_signals"] == 1
    assert report["model"]["skipped_signals"] == 0
    assert set(report["model"]["skip_reasons"].keys()) == {
        "missing_match_id", "missing_selection", "missing_signal_time", "missing_max_entry_decimal"}
    assert report["model"]["legacy_execution_mode_null_count"] == 1
    assert "friend_picks_total" in report["friend"]


# --------------------------------------------------- 6: report separates sources

def test_paper_trades_report_separates_model_and_friend():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
    ])
    db.commit()
    pred = _legacy_prediction(db, m, action="WAIT", current_decimal=2.20)
    paper_trade.simulate_model_candidate(db, pred.id)

    rep = paper_trade.report(db)
    assert rep["model_trades"] == len(paper_trade.DELAYS_SECONDS)
    assert rep["friend_trades"] == 0
    assert "MODEL" in rep["by_source"] and "FRIEND" in rep["by_source"]
    assert rep["by_source"]["MODEL"]["total_trades"] == len(paper_trade.DELAYS_SECONDS)
    assert rep["by_source"]["FRIEND"]["total_trades"] == 0


# --------------------------------------------------- 7: gates expose model_n/friend_n

def test_gates_expose_model_n_and_friend_n():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add(_snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300))
    m.home_score, m.away_score, m.winner = 1, 0, "home"
    db.commit()
    pred = _legacy_prediction(db, m, action="WAIT", current_decimal=2.20)
    paper_trade.simulate_model_candidate(db, pred.id)

    execution = profit_gates.execution_gate(db)
    risk = profit_gates.risk_gate(db)
    assert "model_n" in execution and "friend_n" in execution
    assert "model_n" in risk and "friend_n" in risk
    assert execution["model_n"] >= 0
    assert execution["friend_n"] == 0


# --------------------------------------------------- 8: winner-edge basics

def test_winner_edge_returns_model_winner_accuracy_and_favorite_baseline():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        m = _match(db, start + timedelta(minutes=i), home=f"H{i}", away=f"A{i}")
        db.add_all([
            _snap(m.id, "bet365", "home", 1.50, start + timedelta(minutes=i) - timedelta(minutes=5), 300),
            _snap(m.id, "bet365", "away", 3.00, start + timedelta(minutes=i) - timedelta(minutes=5), 300),
        ])
        m.home_score, m.away_score, m.winner = (1, 0, "home") if i % 2 == 0 else (0, 1, "away")
        db.commit()
        _legacy_prediction(db, m, selection="home", predicted_winner="home",
                          current_decimal=1.50, model_prob=0.6)
    rep = winner_edge.model_report(db)
    assert rep["distinct_samples"] == 5
    assert rep["winner_accuracy_pct"] is not None
    assert rep["favorite_baseline_accuracy_pct"] is not None


# --------------------------------------------------- 9: dedup, not raw rows

def test_winner_edge_dedups_by_match_selection_not_raw_rows():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add(_snap(m.id, "bet365", "home", 2.0, start - timedelta(minutes=10), 600))
    m.home_score, m.away_score, m.winner = 1, 0, "home"
    db.commit()
    # 6 horizons, same (match, selection) -- must collapse to 1 sample.
    for i, horizon_minutes in enumerate([30, 15, 10, 5, 2, 0]):
        _legacy_prediction(db, m, selection="home", predicted_winner="home",
                          prediction_time=start - timedelta(minutes=horizon_minutes),
                          current_decimal=2.0, model_prob=0.55)
    rep = winner_edge.model_report(db)
    assert rep["total_predictions"] == 6
    assert rep["distinct_samples"] == 1  # NOT 6


# --------------------------------------------------- 10: no future leakage

def test_winner_edge_does_not_use_future_result_or_first_live_price():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Pre-kick price at prediction time, plus a drastically different LIVE
    # price after kickoff -- devig/market_implied must only see the pre-kick one.
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "away", 1.90, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "home", 9.00, start + timedelta(seconds=5), -5, "live"),
    ])
    m.home_score, m.away_score, m.winner = 0, 1, "away"
    db.commit()
    pred = _legacy_prediction(db, m, selection="home", predicted_winner="home",
                              prediction_time=start - timedelta(minutes=5), current_decimal=2.20)
    samples = winner_edge._model_samples(db)
    s = next(x for x in samples if x["prediction_id"] == pred.id)
    assert s["current_decimal"] == 2.20  # not the live 9.00
    assert s["devigged_prob"] is not None
    assert abs(s["market_implied_prob"] - odds_math.implied_prob(2.20)) < 1e-3  # engine rounds to 4dp


# --------------------------------------------------- 11: ROI uses paper trades only

def test_roi_by_delay_uses_paper_trades_only():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add(_snap(m.id, "bet365", "home", 2.0, start, 0, "live"))
    m.home_score, m.away_score, m.winner = 1, 0, "home"
    db.commit()
    pred = _legacy_prediction(db, m, selection="home", predicted_winner="home",
                              prediction_time=start, current_decimal=2.0, max_entry=1.5)
    paper_trade.simulate_model_candidate(db, pred.id)
    trades = db.scalars(select(PaperTrade).where(
        PaperTrade.signal_source == "MODEL")).all()
    roi = winner_edge._roi_by_delay(db, trades)
    # price 2.0 >= max_entry 1.5 -> survived -> filled -> settled (home won)
    assert roi["0"] is not None
    assert roi["0"] > 0  # won at decimal 2.0 -> positive ROI


def test_seed_data_off_after_profit_core_repair():
    db = _db()
    s = db.get(Settings, 1)
    assert s.include_seed_data is False
