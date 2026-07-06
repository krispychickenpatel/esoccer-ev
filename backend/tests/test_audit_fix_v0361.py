"""v0.3.6.1 Audit Fix Cleanup: signal-gate sample dedup, friend-pick error
buckets, BOOK_UNAVAILABLE wiring, friend-pick verify_integrity, hash v1/v2
compatibility, book_seen proxy labeling."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import friend_picks, odds_math, profit_gates
from app.models import (Base, BookmakerCoverage, FriendPick, FriendPickScore, Match,
                        OddsSnapshot, Player, PredictionLedger, PredictionReality,
                        PredictionScore, Settings)


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1, min_ev_pct=1.0))
    db.commit()
    return db


def _match(db, start, league="Esoccer Battle - 8 mins play", home="H", away="A"):
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


def _reality(match_id, selection, shortened, tier="gold", first_live_after_s=20.0):
    return PredictionReality(match_id=match_id, sportsbook="bet365", market="ML_3WAY",
                             selection=selection, last_pre_decimal=2.2, first_live_decimal=1.95 if shortened else 2.4,
                             actual_shortened=shortened, dataset_tier=tier,
                             first_live_after_s=first_live_after_s, warnings_json="[]")


# --------------------------------------------------------- Fix 1: signal gate dedup

def test_repeated_horizons_same_match_selection_count_as_one_sample():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    reality = _reality(m.id, "home", shortened=True)
    db.add(reality); db.flush()

    # 6 horizons, same match/selection, ALL steam_direction_correct=True --
    # this must collapse to exactly 1 distinct sample, not 6.
    for i, horizon in enumerate(["T-30m", "T-15m", "T-10m", "T-5m", "T-2m", "KICKOFF"]):
        pred = PredictionLedger(
            match_id=m.id, horizon_label=horizon, prediction_time=start - timedelta(minutes=30 - i * 5),
            scheduled_start=start, model_version="v", sportsbook="bet365", market="ML_3WAY",
            selection="home", current_decimal=2.2, immutable_hash=f"h{i}")
        db.add(pred); db.flush()
        db.add(PredictionScore(prediction_id=pred.id, reality_id=reality.id,
                               steam_direction_correct=True, error_bucket="OK"))
    db.commit()

    sample = profit_gates._steam_sample(db, "model")
    assert len(sample) == 6  # raw rows
    dedup = profit_gates._dedup_by_match_selection(sample)
    assert len(dedup) == 1  # distinct samples
    assert list(dedup.values()) == [True]


def test_signal_gate_reports_raw_rows_and_distinct_samples_separately():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    # 40 raw rows across only 10 distinct (match,selection) pairs (4 horizons each)
    # -- enough distinct samples to clear MIN_SIGNAL_SAMPLE=30? No -- use 32 distinct
    # so both raw_rows (128) and distinct_samples (32) clear/don't-clear correctly.
    for i in range(32):
        m = _match(db, start + timedelta(minutes=i), home=f"H{i}", away=f"A{i}")
        reality = _reality(m.id, "home", shortened=(i % 2 == 0))
        db.add(reality); db.flush()
        for h_i, horizon in enumerate(["T-10m", "T-5m", "T-2m", "KICKOFF"]):
            pred = PredictionLedger(
                match_id=m.id, horizon_label=horizon, prediction_time=start + timedelta(minutes=i, seconds=h_i),
                scheduled_start=start + timedelta(minutes=i), model_version="v", sportsbook="bet365",
                market="ML_3WAY", selection="home", current_decimal=2.2, immutable_hash=f"h{i}-{h_i}")
            db.add(pred); db.flush()
            db.add(PredictionScore(prediction_id=pred.id, reality_id=reality.id,
                                   steam_direction_correct=(i % 2 == 0), error_bucket="OK"))
    db.commit()

    result = profit_gates.signal_gate(db, "model")
    assert result["raw_rows"] == 32 * 4
    assert result["distinct_samples"] == 32
    assert result["n"] == 32  # gate's n MUST equal distinct_samples, not raw_rows
    assert result["n"] != result["raw_rows"]


def test_gate_status_uses_distinct_samples_not_raw_rows():
    """29 distinct (match,selection) samples, each repeated 20x (580 raw rows)
    must still be NOT ENOUGH DATA -- raw_rows alone would wrongly clear the
    n>=30 threshold."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(29):
        m = _match(db, start + timedelta(minutes=i), home=f"H{i}", away=f"A{i}")
        reality = _reality(m.id, "home", shortened=True)
        db.add(reality); db.flush()
        for h_i in range(20):
            pred = PredictionLedger(
                match_id=m.id, horizon_label=f"H{h_i}", prediction_time=start + timedelta(minutes=i, seconds=h_i),
                scheduled_start=start + timedelta(minutes=i), model_version="v", sportsbook="bet365",
                market="ML_3WAY", selection="home", current_decimal=2.2, immutable_hash=f"h{i}-{h_i}")
            db.add(pred); db.flush()
            db.add(PredictionScore(prediction_id=pred.id, reality_id=reality.id,
                                   steam_direction_correct=True, error_bucket="OK"))
    db.commit()
    result = profit_gates.signal_gate(db, "model")
    assert result["raw_rows"] == 29 * 20
    assert result["distinct_samples"] == 29
    assert result["status"] == "NOT ENOUGH DATA"  # would wrongly be scoreable at raw_rows=580


def test_signal_gate_no_future_leakage():
    """Baseline favorite lookup must only use pre-match snapshots at-or-before
    kickoff -- confirms the dedup fix didn't introduce a leakage regression."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Live/post-kickoff snapshot with a drastically different price --
    # must NOT influence favorite_selection().
    db.add_all([
        _snap(m.id, "bet365", "home", 1.50, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "away", 5.00, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "home", 9.00, start + timedelta(seconds=5), -5, "live"),
    ])
    db.commit()
    fav = friend_picks.favorite_selection(db, m.id, "bet365", "ML_3WAY", start)
    assert fav == "home"  # still the pre-kickoff favorite, live blowout ignored


# --------------------------------------------------------- Fix 2/3: error buckets

def test_classify_bucket_clean_win_lands_in_valid_spec_bucket():
    bucket = friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=True, steam_direction_correct=True,
        proxy_clv_pct=2.5, entry_price_survived=True)
    assert bucket in friend_picks.ERROR_BUCKETS
    assert bucket == "RESULT_RIGHT_NO_MARKET_EDGE"
    assert bucket != "OK"


def test_classify_bucket_bad_price():
    bucket = friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=True, steam_direction_correct=True,
        proxy_clv_pct=-3.0, entry_price_survived=True)
    assert bucket == "CORRECT_SIDE_BAD_PRICE"


def test_classify_bucket_missing_data():
    bucket = friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=None, steam_direction_correct=None,
        proxy_clv_pct=None, entry_price_survived=None)
    assert bucket == "DATA_UNAVAILABLE"


def test_classify_bucket_missed_execution_window():
    bucket = friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=True, steam_direction_correct=True,
        proxy_clv_pct=1.0, entry_price_survived=False)
    assert bucket == "MISSED_EXECUTION_WINDOW"


def test_classify_bucket_wrong_side_and_steam_right_result_wrong():
    assert friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=False, steam_direction_correct=False,
        proxy_clv_pct=None, entry_price_survived=None) == "WRONG_SIDE"
    assert friend_picks._classify_friend_error_bucket(
        book_check="unknown", winner_correct=False, steam_direction_correct=True,
        proxy_clv_pct=None, entry_price_survived=None) == "STEAM_RIGHT_RESULT_WRONG"


def test_no_ok_bucket_can_appear_across_all_input_combinations():
    for winner_correct in (True, False, None):
        for steam in (True, False, None):
            for clv in (-5.0, 0.0, 5.0, None):
                for survived in (True, False, None):
                    for book_check in ("verified", "unavailable", "unknown"):
                        bucket = friend_picks._classify_friend_error_bucket(
                            book_check=book_check, winner_correct=winner_correct,
                            steam_direction_correct=steam, proxy_clv_pct=clv,
                            entry_price_survived=survived)
                        assert bucket in friend_picks.ERROR_BUCKETS
                        assert bucket != "OK"


def test_book_unavailable_wired_when_coverage_proves_it():
    db = _db()
    db.add(BookmakerCoverage(source_name="fanduel", status="EMPTY", execution_candidate=False))
    db.commit()
    check = friend_picks._check_book_coverage(db, "FanDuel app")
    assert check == "unavailable"
    bucket = friend_picks._classify_friend_error_bucket(
        book_check=check, winner_correct=True, steam_direction_correct=True,
        proxy_clv_pct=2.0, entry_price_survived=True)
    assert bucket == "BOOK_UNAVAILABLE"


def test_book_unavailable_never_guessed_without_a_scan():
    db = _db()
    # No BookmakerCoverage rows at all -- must not guess unavailability.
    check = friend_picks._check_book_coverage(db, "FanDuel app")
    assert check == "unknown"
    bucket = friend_picks._classify_friend_error_bucket(
        book_check=check, winner_correct=None, steam_direction_correct=None,
        proxy_clv_pct=None, entry_price_survived=None)
    assert bucket == "DATA_UNAVAILABLE"
    assert bucket != "BOOK_UNAVAILABLE"


def test_book_check_verified_when_scan_shows_works():
    db = _db()
    db.add(BookmakerCoverage(source_name="bet365", status="WORKS", execution_candidate=False))
    db.commit()
    assert friend_picks._check_book_coverage(db, "bet365 app") == "verified"


# --------------------------------------------------------- Fix 4/5: verify_integrity + hash v1/v2

def test_verify_integrity_valid_pick_passes():
    db = _db()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
        "reason": "friend says so", "confidence": "high",
    })
    report = friend_picks.verify_integrity(db)
    assert report["checked"] == 1
    assert report["valid"] == 1
    assert report["invalid"] == 0
    assert pick.id not in report["invalid_ids"]


def test_verify_integrity_tampered_reason_fails_for_v2_row():
    db = _db()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
        "reason": "original reason", "confidence": "high",
    })
    pick.reason = "TAMPERED"
    db.commit()
    report = friend_picks.verify_integrity(db)
    assert report["invalid"] == 1
    assert pick.id in report["invalid_ids"]


def test_verify_integrity_tampered_confidence_fails_for_v2_row():
    db = _db()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app", "confidence": "low",
    })
    pick.confidence = "high"
    db.commit()
    report = friend_picks.verify_integrity(db)
    assert report["invalid"] == 1


def test_verify_integrity_tampered_provider_event_id_fails_for_v2_row():
    db = _db()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
        "provider_event_id": "abc123",
    })
    pick.provider_event_id = "TAMPERED"
    db.commit()
    report = friend_picks.verify_integrity(db)
    assert report["invalid"] == 1


def test_legacy_v1_hashed_row_still_verifies_and_does_not_break_startup():
    """Simulates a pre-v0.3.6.1 row hashed under the narrower v1 payload."""
    db = _db()
    now = datetime(2026, 1, 1, 12, 0, 0)
    v1_payload = friend_picks._freeze_payload_v1(
        now, now, "H", "A", "home", 2.10, "bet365 app", "", None)
    legacy = FriendPick(
        created_at=now, pick_timestamp=now, effective_known_at=now, is_backfilled=False,
        league="", home_name="H", away_name="A", kickoff_time=None, pick_side="home",
        odds_at_pick_american=110, odds_at_pick_decimal=2.10, book_seen="bet365 app",
        reason="", confidence=None, resolution_status="PENDING", scoring_status="pending",
        immutable_hash=friend_picks._freeze_hash(v1_payload))
    db.add(legacy); db.commit()

    report = friend_picks.verify_integrity(db)
    assert report["checked"] == 1
    assert report["valid"] == 1
    assert report["invalid"] == 0
    assert "hash_fields_v1" in report and "hash_fields_v2" in report


def test_correction_row_verifies_independently_of_original():
    db = _db()
    original = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
    })
    correction = friend_picks.correct_friend_pick(db, original.id, {"odds_at_pick_decimal": 2.30})
    report = friend_picks.verify_integrity(db)
    assert report["checked"] == 2
    assert report["valid"] == 2
    assert report["invalid"] == 0
    # Tampering the correction must not affect the original's validity.
    correction.reason = "TAMPERED"
    db.commit()
    report2 = friend_picks.verify_integrity(db)
    assert report2["invalid"] == 1
    assert correction.id in report2["invalid_ids"]
    assert original.id not in report2["invalid_ids"]


# --------------------------------------------------------- Fix 6: book_seen proxy labeling

def test_friend_pick_scored_via_reference_feed_marked_as_proxy():
    from app.engines.prediction_lab import capture_reality
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "away", 1.90, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "home", 1.95, start + timedelta(seconds=4), -4, "live"),
        _snap(m.id, "bet365", "away", 2.05, start + timedelta(seconds=4), -4, "live"),
    ])
    m.home_score, m.away_score, m.winner = 3, 1, "home"
    db.commit()
    capture_reality(db)  # produces the real PredictionReality row scoring reads from
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.20, "book_seen": "FanDuel app",  # NOT bet365
        "kickoff_time": start, "league": "Esoccer Battle - 8 mins play",
    })
    friend_picks.score_friend_pick(db, pick)
    out = friend_picks.pick_out(db, pick)
    assert out["scoring_price_source"] == "bet365"
    assert out["is_reference_feed_proxy"] is True  # scored via bet365, friend saw FanDuel
    assert out["book_verified_for_execution"] is False  # no coverage scan proved FanDuel usable


def test_scoring_does_not_claim_execution_availability_without_coverage():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "home", 1.95, start + timedelta(seconds=4), -4, "live"),
    ])
    m.home_score, m.away_score, m.winner = 3, 1, "home"
    db.commit()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.20, "book_seen": "bet365 app",
        "kickoff_time": start, "league": "Esoccer Battle - 8 mins play",
    })
    friend_picks.score_friend_pick(db, pick)
    out = friend_picks.pick_out(db, pick)
    # bet365 is the reference feed and is never execution_candidate by design.
    assert out["book_verified_for_execution"] is False


# --------------------------------------------------------- Seed data / regressions

def test_seed_data_still_off_after_audit_fixes():
    db = _db()
    s = db.get(Settings, 1)
    assert s.include_seed_data is False
    assert json.loads(s.sportsbooks_tracked) == ["bet365"]
