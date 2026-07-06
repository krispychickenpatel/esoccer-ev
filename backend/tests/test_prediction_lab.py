from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import odds_math
from app.engines.prediction_lab import (capture_reality, freeze_match_horizon,
                                        model_comparison, score_predictions)
from app.models import Base, Match, OddsSnapshot, Player, PredictionLedger, PredictionScore, Settings


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1, min_ev_pct=1.0))
    db.commit()
    return db


def _snap(match_id, book, selection, dec, at, s2k, phase="pre_match"):
    return OddsSnapshot(
        match_id=match_id,
        sportsbook=book,
        market="ML_3WAY",
        selection=selection,
        line=None,
        american_odds=odds_math.decimal_to_american(dec),
        decimal_odds=dec,
        implied_prob=odds_math.implied_prob(dec),
        collected_at=at,
        seconds_to_kickoff=s2k,
        phase=phase,
        data_source="betsapi",
        verification_status="api_verified",
    )


def test_prediction_lab_freezes_captures_scores():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="BLITZ", league="L")
    away = Player(name="ALIBI", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(start_time=start, league="L", home_player_id=home.id, away_player_id=away.id,
                  source="betsapi", verification_status="api_verified")
    db.add(match); db.flush()
    db.add_all([
        _snap(match.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(match.id, "bet365", "away", 1.90, start - timedelta(minutes=5), 300),
    ])
    db.commit()

    frozen = freeze_match_horizon(db, match, "T-5m", prediction_time=start - timedelta(minutes=5))
    assert len(frozen) == 2
    assert db.query(PredictionLedger).count() == 2

    # Finish the match and add first-live rows. Home shortens, away drifts.
    match.home_score = 3
    match.away_score = 1
    match.winner = "home"
    db.add_all([
        _snap(match.id, "bet365", "home", 1.95, start + timedelta(seconds=4), -4, "live"),
        _snap(match.id, "bet365", "away", 2.05, start + timedelta(seconds=4), -4, "live"),
    ])
    db.commit()

    reality = capture_reality(db)
    assert reality["reality_rows_touched"] == 2
    scored = score_predictions(db)
    assert scored["scored"] == 2
    assert db.query(PredictionScore).count() == 2

    report = model_comparison(db)
    assert report["groups"][0]["scored_n"] == 2
    assert report["groups"][0]["horizon_label"] == "T-5m"


def test_late_freeze_cannot_see_own_live_ticks():
    """Leakage regression: freezing with a historical prediction_time after the
    match already has live snapshots + a result must NOT let the movement
    signal or steam history consume this match's own first-live jump."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="P1", league="L")
    away = Player(name="P2", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(start_time=start, league="L", home_player_id=home.id, away_player_id=away.id,
                  source="betsapi", verification_status="api_verified")
    db.add(match); db.flush()
    # Pre + live snapshots and final result ALREADY exist before freezing.
    db.add_all([
        _snap(match.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(match.id, "bet365", "home", 1.80, start + timedelta(seconds=3), -3, "live"),
    ])
    match.home_score, match.away_score, match.winner = 3, 1, "home"
    db.commit()

    frozen = freeze_match_horizon(db, match, "T-5m",
                                  prediction_time=start - timedelta(minutes=5))
    assert frozen, "expected a frozen row from the pre-kickoff snapshot"
    import json
    feats = json.loads(frozen[0].features_json)
    movement = next(s for s in feats["signals"] if s["name"] == "movement")
    # With only one pre-kickoff snapshot visible at prediction_time, the
    # movement signal must report no usable timeline — if it sees the live
    # jump (2.20 -> 1.80) the leakage guard is broken.
    assert movement["quality"] == 0.0
    assert "first-live jump" not in movement["reason"]
    # Steam history must also be empty: the only finished match is this one,
    # and its pairs settle after prediction_time.
    assert feats["steam"]["historical_sample"] == 0


def test_steam_history_bounded_by_as_of():
    """A history match that kicked off AFTER prediction_time must not feed
    steam features, even though it is finished in the DB right now."""
    from app.engines.steam import steam_prediction_for_snapshot
    db = _db()
    start_hist = datetime(2026, 1, 1, 11, 0, 0)
    start_pred = datetime(2026, 1, 1, 12, 0, 0)
    h1 = Player(name="A", league="L"); h2 = Player(name="B", league="L")
    db.add_all([h1, h2]); db.flush()
    hist = Match(start_time=start_hist, league="L", home_player_id=h1.id, away_player_id=h2.id,
                 source="betsapi", home_score=2, away_score=0, winner="home")
    pred = Match(start_time=start_pred, league="L", home_player_id=h1.id, away_player_id=h2.id,
                 source="betsapi")
    db.add_all([hist, pred]); db.flush()
    db.add_all([
        _snap(hist.id, "bet365", "home", 2.10, start_hist - timedelta(minutes=2), 120),
        _snap(hist.id, "bet365", "home", 1.90, start_hist + timedelta(seconds=5), -5, "live"),
    ])
    snap = _snap(pred.id, "bet365", "home", 2.20, start_pred - timedelta(minutes=5), 300)
    db.add(snap); db.commit()

    # as_of BEFORE the history match kicked off -> no history visible.
    out_blind = steam_prediction_for_snapshot(db, pred, snap,
                                              as_of=start_hist - timedelta(minutes=10))
    assert out_blind["historical_sample"] == 0
    # as_of after history settled -> pair visible.
    out_seen = steam_prediction_for_snapshot(db, pred, snap,
                                             as_of=start_pred - timedelta(minutes=5))
    assert out_seen["historical_sample"] > 0


def test_ledger_integrity_detects_tampering():
    from app.engines.prediction_lab import verify_integrity
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="X", league="L"); away = Player(name="Y", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(start_time=start, league="L", home_player_id=home.id, away_player_id=away.id,
                  source="betsapi")
    db.add(match); db.flush()
    db.add(_snap(match.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300))
    db.commit()
    frozen = freeze_match_horizon(db, match, "T-5m", prediction_time=start - timedelta(minutes=5))
    assert frozen
    assert verify_integrity(db)["mismatched"] == 0
    # Tamper with the frozen row -> must be detected.
    frozen[0].action = "BET" if frozen[0].action != "BET" else "PASS"
    db.commit()
    rep = verify_integrity(db)
    assert rep["mismatched"] == 1
    assert frozen[0].id in rep["mismatched_ids"]


def test_allow_late_freezes_only_nearest_horizon(monkeypatch):
    """allow_late must not stamp every passed horizon label with 'now'."""
    from app.engines import prediction_lab as pl
    db = _db()
    now = datetime(2026, 1, 1, 12, 0, 0)
    monkeypatch.setattr(pl, "_now", lambda: now)
    home = Player(name="M", league="L"); away = Player(name="N", league="L")
    db.add_all([home, away]); db.flush()
    # Match starts in 4 minutes: nearest honest horizon is T-5m.
    match = Match(start_time=now + timedelta(minutes=4), league="L",
                  home_player_id=home.id, away_player_id=away.id, source="betsapi")
    db.add(match); db.flush()
    db.add(_snap(match.id, "bet365", "home", 2.20, now - timedelta(minutes=1), 300))
    db.commit()
    out = pl.freeze_due_predictions(db, tolerance_seconds=30, allow_late=True)
    assert out["per_horizon"] == {"T-5m": 1}, out
