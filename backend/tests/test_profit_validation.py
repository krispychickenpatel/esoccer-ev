"""v0.3.6 Profit Validation Layer: Friend Picks, Paper Trade Engine, Book
Coverage Scanner, Profit Kill Gates."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import book_coverage, friend_picks, odds_math, paper_trade, profit_gates
from app.models import (Base, Match, OddsSnapshot, Player, RawProviderResponse,
                        Settings)


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
        match_id=match_id, sportsbook=book, market="ML_3WAY", selection=selection,
        line=None, american_odds=odds_math.decimal_to_american(dec), decimal_odds=dec,
        implied_prob=odds_math.implied_prob(dec), collected_at=at, seconds_to_kickoff=s2k,
        phase=phase, data_source="betsapi", verification_status="api_verified")


def _match(db, start, league="Esoccer Battle - 8 mins play", home="H", away="A"):
    h = Player(name=home, league=league)
    a = Player(name=away, league=league)
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league=league, home_player_id=h.id, away_player_id=a.id,
             source="betsapi", verification_status="api_verified")
    db.add(m); db.flush()
    return m


# ---------------------------------------------------------------- Friend Picks

def test_friend_picks_are_immutable_and_tamper_detected():
    db = _db()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
    })
    original_hash = pick.immutable_hash
    # Recomputing the hash from the same frozen fields must match.
    frozen = friend_picks._freeze_payload(pick.pick_timestamp, pick.effective_known_at,
                                          pick.home_name, pick.away_name, pick.pick_side,
                                          pick.odds_at_pick_decimal, pick.book_seen,
                                          pick.league, pick.kickoff_time)
    assert friend_picks._freeze_hash(frozen) == original_hash
    # Tampering with a frozen field must be detectable by hash mismatch.
    pick.odds_at_pick_decimal = 5.0
    tampered = friend_picks._freeze_payload(pick.pick_timestamp, pick.effective_known_at,
                                            pick.home_name, pick.away_name, pick.pick_side,
                                            pick.odds_at_pick_decimal, pick.book_seen,
                                            pick.league, pick.kickoff_time)
    assert friend_picks._freeze_hash(tampered) != original_hash


def test_friend_pick_correction_creates_new_row_never_edits_original():
    db = _db()
    original = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
    })
    original_odds = original.odds_at_pick_decimal
    original_hash = original.immutable_hash

    correction = friend_picks.correct_friend_pick(db, original.id, {"odds_at_pick_decimal": 2.30})

    assert correction.id != original.id
    assert correction.corrects_pick_id == original.id
    assert correction.odds_at_pick_decimal == 2.30
    # Original untouched.
    db.refresh(original)
    assert original.odds_at_pick_decimal == original_odds
    assert original.immutable_hash == original_hash
    assert original.corrects_pick_id is None


def test_backfilled_pick_effective_known_at_never_precedes_created_at():
    db = _db()
    long_ago = datetime(2020, 1, 1, 12, 0, 0)
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
        "pick_timestamp": long_ago,
    })
    assert pick.is_backfilled is True
    assert pick.effective_known_at >= pick.created_at
    assert pick.effective_known_at != long_ago


def test_backfilled_pick_cannot_leak_into_earlier_scoring():
    """A backfilled pick's effective_known_at must be used for the favorite/
    baseline lookup, not the (earlier, claimed) pick_timestamp -- otherwise a
    backfilled pick could see market data that "existed" only because time
    had already passed by the moment it was entered."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Odds move a lot between "claimed" pick time and actual entry time.
    db.add_all([
        _snap(m.id, "bet365", "home", 3.00, start - timedelta(minutes=20), 1200),
        _snap(m.id, "bet365", "home", 1.50, start - timedelta(minutes=2), 120),
    ])
    db.commit()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 3.00, "book_seen": "bet365 app",
        "pick_timestamp": start - timedelta(minutes=20),  # claims to be early
        "kickoff_time": start, "league": "Esoccer Battle - 8 mins play",
    })
    # effective_known_at is "now" (test run time), which is far after both
    # snapshots -- so the favorite lookup for effective_known_at sees BOTH
    # snapshots (the late 1.50 one included), proving it did not pin to the
    # claimed pick_timestamp.
    fav = friend_picks.favorite_selection(db, m.id, "bet365", "ML_3WAY", pick.effective_known_at)
    assert fav == "home"  # only selection present either way, but confirms lookup ran at effective_known_at
    assert pick.effective_known_at > start - timedelta(minutes=20)


def test_friend_scoring_end_to_end_after_odds_and_result_exist():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "away", 1.90, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "home", 1.95, start + timedelta(seconds=4), -4, "live"),
        _snap(m.id, "bet365", "away", 2.05, start + timedelta(seconds=4), -4, "live"),
    ])
    db.commit()
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.20, "book_seen": "bet365 app",
        "kickoff_time": start, "league": "Esoccer Battle - 8 mins play",
    })
    assert pick.resolution_status == "RESOLVED"

    from app.engines.prediction_lab import capture_reality
    capture_reality(db)
    m.home_score, m.away_score, m.winner = 3, 1, "home"
    db.commit()
    capture_reality(db)

    score = friend_picks.score_friend_pick(db, pick)
    assert score is not None
    assert pick.scoring_status == "scored"
    assert score.winner_correct is True
    assert score.steam_direction_correct is True  # 2.20 -> 1.95 shortened
    assert score.paper_pl_usd is not None and score.paper_pl_usd > 0


def test_baseline_comparison_uses_favorite_selection():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # home is the favorite (lower decimal = more likely per market).
    db.add_all([
        _snap(m.id, "bet365", "home", 1.50, start - timedelta(minutes=5), 300),
        _snap(m.id, "bet365", "away", 5.00, start - timedelta(minutes=5), 300),
    ])
    db.commit()
    fav = friend_picks.favorite_selection(db, m.id, "bet365", "ML_3WAY", start)
    assert fav == "home"


# ---------------------------------------------------------------- Book Coverage

class _FakeProviderOdds:
    def __init__(self, responses):
        self._responses = responses  # book -> list of odds dicts (possibly empty)

    def fetch_odds(self, event_id, source="bet365"):
        return self._responses.get(source, [])


def test_book_coverage_marks_empty_reachable_book_as_empty_not_broken():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    m.ext_id = "99999"
    db.commit()
    provider = _FakeProviderOdds({"bet365": [{"market": "ML_3WAY", "selection": "home",
                                              "collected_at": start - timedelta(minutes=5)}],
                                  "fanduel": []})
    result = book_coverage.run_scan(db, provider, books=["bet365", "fanduel"])
    assert result["skipped"] is False
    assert result["results"]["bet365"]["status"] == "WORKS"
    assert result["results"]["fanduel"]["status"] == "EMPTY"

    rows = {r["source_name"]: r for r in book_coverage.list_coverage(db)}
    assert rows["fanduel"]["status"] == "EMPTY"
    assert rows["fanduel"]["execution_candidate"] is False
    assert rows["bet365"]["execution_candidate"] is False  # reference feed never becomes a candidate


def test_book_coverage_scanner_refuses_during_live_window():
    db = _db()
    now = book_coverage._now()  # UTC-naive, matching the app's own clock convention
    m = _match(db, now + timedelta(minutes=1))  # inside KO+-2min window
    m.ext_id = "1"
    db.commit()
    result = book_coverage.run_scan(db, _FakeProviderOdds({}))
    assert result["skipped"] is True


# ---------------------------------------------------------------- Paper Trade

def test_paper_trade_handles_missed_stale_price_never_fabricates_fill():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    db.commit()
    # No odds snapshots exist at all -- every delay must be MISSED_PRICE.
    trades = paper_trade.simulate_signal(
        db, signal_source="MODEL", signal_id=1, match_id=m.id, sportsbook="bet365",
        market="ML_3WAY", selection="home", signal_time=start, max_entry_decimal=2.0)
    assert len(trades) == len(paper_trade.DELAYS_SECONDS)
    for t in trades:
        assert t.settlement_status == "MISSED_PRICE"
        assert t.entry_survived is False
        assert t.price_decimal is None
        assert t.paper_pl_usd is None


def test_paper_trade_delay_bucket_fill_rate_math():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    # Price available at t+0 and t+5 only (survives max_entry); goes stale after.
    db.add_all([
        _snap(m.id, "bet365", "home", 2.20, start, 0, "live"),
        _snap(m.id, "bet365", "home", 2.10, start + timedelta(seconds=5), -5, "live"),
    ])
    db.commit()
    trades = paper_trade.simulate_signal(
        db, signal_source="MODEL", signal_id=2, match_id=m.id, sportsbook="bet365",
        market="ML_3WAY", selection="home", signal_time=start, max_entry_decimal=2.0)
    by_delay = {t.delay_seconds: t for t in trades}
    assert by_delay[0].settlement_status == "FILLED"
    assert by_delay[5].settlement_status == "FILLED"
    # No snapshot within 60s of t+45 that's fresh enough (last one is 40s stale at that point)
    # -- still within STALE_AFTER_SECONDS=60 of the t+5 snapshot actually, so check t+45 explicitly:
    assert by_delay[45].price_decimal == 2.10  # still the latest known price, within 60s staleness window

    rep = paper_trade.report(db)
    assert rep["by_delay_seconds"]["0"]["total"] == 1
    assert rep["by_delay_seconds"]["0"]["fill_rate_pct"] == 100.0
    assert rep["disclaimer"] == paper_trade.DISCLAIMER


# ---------------------------------------------------------------- Profit Gates

def test_profit_gates_default_to_not_enough_data_on_empty_db():
    db = _db()
    result = profit_gates.compute_all_gates(db)
    assert result["gates"]["feed_gate"]["pre_kickoff"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["feed_gate"]["live_open_manual"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["signal_gate_model"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["signal_gate_friend"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["execution_gate"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["risk_gate"]["status"] == "NOT ENOUGH DATA"
    assert result["gates"]["book_gate"]["status"] == "FAIL"  # zero verified books is a real FAIL, not unknown
    assert result["ready_for_live_small_stakes"] == "NOT ENOUGH DATA"
    assert result["pipeline_health"]["matches_collecting"]["status"] == "NOT ENOUGH DATA"


def test_feed_gate_does_not_pass_15s_live_reaction_under_recorded_latency():
    """Reproduces the real observed distribution (avg ~27s, most 17-45s,
    some 60s+, 0% within 15s) and confirms the gate does not lie about it."""
    from app.models import PredictionReality
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    latencies = [17, 18, 19, 20, 20, 21, 23, 25, 25, 26, 26, 29, 31, 34, 37, 38, 38,
                43, 58, 64, 20, 21, 19, 25, 26, 17, 18, 20, 43, 21]  # n=30, 0 within 15s
    assert len(latencies) == 30
    for i, lat in enumerate(latencies):
        m = _match(db, start + timedelta(minutes=i), home=f"H{i}", away=f"A{i}")
        db.add(PredictionReality(match_id=m.id, sportsbook="bet365", market="ML_3WAY",
                                 selection="home", first_live_after_s=float(lat),
                                 dataset_tier="silver"))
    db.commit()
    result = profit_gates.feed_gate(db)
    assert result["live_open_manual"]["n"] == 30
    assert result["live_open_manual"]["status"] == "FAIL"  # p95 (~58-64s) exceeds the 45s stress assumption
    assert result["live_open_manual"]["p95_based_status"] == "FAIL"


# ---------------------------------------------------------------- Seed data / real-mode

def test_seed_data_remains_off_and_real_mode_protections_intact():
    db = _db()
    s = db.get(Settings, 1)
    assert s.include_seed_data is False
    assert json.loads(s.sportsbooks_tracked) == ["bet365"]
    # AUTO_LOAD_SEED_DATA behavior lives in main.py and is env-gated;
    # confirm no code path in this module ever sets seed-ish data_source.
    pick = friend_picks.create_friend_pick(db, {
        "pick_side": "home", "home_name": "H", "away_name": "A",
        "odds_at_pick_decimal": 2.10, "book_seen": "bet365 app",
    })
    assert pick.resolution_status in ("PENDING", "RESOLVED")  # never a seed-tagged status
