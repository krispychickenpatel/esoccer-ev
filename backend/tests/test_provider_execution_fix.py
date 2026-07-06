"""v0.3.5 Provider Execution Fix: ended-results ingestion, first-live
priority ordering, fanduel no-op handling, and seed-data-off defaults."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import odds_math
from app.engines.prediction_lab import dashboard, freeze_match_horizon
from app.engines.shadow import data_health
from app.connectors.betsapi_provider import sportsbook_empty_stats
from app.models import (Base, Match, OddsSnapshot, Player, RawProviderResponse,
                        Settings)
from app.services import poller


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


class _FakeProvider:
    """Stand-in for BetsApiProvider.fetch_results() in tests -- no network."""
    def __init__(self, ended_events):
        self._ended = ended_events

    def fetch_results(self, day=None):
        return self._ended


def test_ended_result_ingestion_updates_match_scores():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="BLITZ", league="L")
    away = Player(name="ALIBI", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(ext_id="99001", start_time=start, league="Esoccer Battle - 8 mins play",
                  home_player_id=home.id, away_player_id=away.id, source="betsapi")
    db.add(match); db.commit()
    assert match.home_score is None

    ended = [{
        "ext_id": "99001", "start_time": start, "league": "Esoccer Battle - 8 mins play",
        "home_player": "BLITZ", "away_player": "ALIBI", "home_score": 3, "away_score": 1,
        "ht_home_score": None, "ht_away_score": None, "duration_min": None,
        "winner": None, "source": "betsapi",
    }]
    report = poller.ingest_ended_results(db, _FakeProvider(ended), ["Esoccer Battle - 8 mins play"])

    db.refresh(match)
    assert match.home_score == 3
    assert match.away_score == 1
    assert match.winner == "home"
    assert report["ended_events_fetched"] == 1
    assert report["matched_to_existing_matches"] == 1
    assert report["scores_updated"] == 1
    assert report["unmatched_ended_events"] == 0


def test_ended_result_ingestion_never_overwrites_score_with_null():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="P1", league="L"); away = Player(name="P2", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(ext_id="99002", start_time=start, league="Esoccer Battle - 8 mins play",
                  home_player_id=home.id, away_player_id=away.id, source="betsapi",
                  home_score=2, away_score=0, winner="home")
    db.add(match); db.commit()

    # A malformed/partial ended row with no score data must not blank a real result.
    ended = [{
        "ext_id": "99002", "start_time": start, "league": "Esoccer Battle - 8 mins play",
        "home_player": "P1", "away_player": "P2", "home_score": None, "away_score": None,
        "ht_home_score": None, "ht_away_score": None, "duration_min": None,
        "winner": None, "source": "betsapi",
    }]
    poller.ingest_ended_results(db, _FakeProvider(ended), ["Esoccer Battle - 8 mins play"])
    db.refresh(match)
    assert match.home_score == 2
    assert match.away_score == 0


def test_unmatched_ended_event_is_not_created_as_new_match():
    db = _db()
    ended = [{
        "ext_id": "no-such-match", "start_time": datetime(2026, 1, 1, 12, 0, 0),
        "league": "Esoccer Battle - 8 mins play", "home_player": "X", "away_player": "Y",
        "home_score": 1, "away_score": 0, "ht_home_score": None, "ht_away_score": None,
        "duration_min": None, "winner": None, "source": "betsapi",
    }]
    report = poller.ingest_ended_results(db, _FakeProvider(ended), ["Esoccer Battle - 8 mins play"])
    assert report["matched_to_existing_matches"] == 0
    assert report["unmatched_ended_events"] == 1
    assert db.query(Match).count() == 0


def test_scored_predictions_leave_pending_after_result_ingestion():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    home = Player(name="BLITZ", league="L")
    away = Player(name="ALIBI", league="L")
    db.add_all([home, away]); db.flush()
    match = Match(ext_id="99003", start_time=start, league="Esoccer Battle - 8 mins play",
                  home_player_id=home.id, away_player_id=away.id,
                  source="betsapi", verification_status="api_verified")
    db.add(match); db.flush()
    db.add_all([
        _snap(match.id, "bet365", "home", 2.20, start - timedelta(minutes=5), 300),
        _snap(match.id, "bet365", "away", 1.90, start - timedelta(minutes=5), 300),
        _snap(match.id, "bet365", "home", 1.95, start + timedelta(seconds=4), -4, "live"),
        _snap(match.id, "bet365", "away", 2.05, start + timedelta(seconds=4), -4, "live"),
    ])
    db.commit()
    freeze_match_horizon(db, match, "T-5m", prediction_time=start - timedelta(minutes=5))

    before = dashboard(db)["totals"]["pending_scores"]
    assert before > 0

    ended = [{
        "ext_id": "99003", "start_time": start, "league": "Esoccer Battle - 8 mins play",
        "home_player": "BLITZ", "away_player": "ALIBI", "home_score": 3, "away_score": 1,
        "ht_home_score": None, "ht_away_score": None, "duration_min": None,
        "winner": None, "source": "betsapi",
    }]
    report = poller.ingest_ended_results(db, _FakeProvider(ended), ["Esoccer Battle - 8 mins play"])

    after = dashboard(db)["totals"]["pending_scores"]
    assert after < before
    assert report["predictions_newly_scored"] > 0
    assert report["scoring_errors"] == 0


def test_fanduel_empty_responses_counted_but_not_fatal():
    db = _db()
    now = datetime(2026, 1, 1, 12, 0, 0)
    empty_payload = json.dumps({"success": 1, "results": {"stats": {}, "odds": {}}})
    rows = [RawProviderResponse(at=now, provider="betsapi", endpoint="/v2/event/odds",
                                status_code=200, sportsbook="fanduel", payload=empty_payload)
            for _ in range(5)]
    rows.append(RawProviderResponse(at=now, provider="betsapi", endpoint="/v2/event/odds",
                                    status_code=200, sportsbook="bet365",
                                    payload=json.dumps({"success": 1, "results": {
                                        "stats": {}, "odds": {"1_1": [{"id": "1", "home_od": "2.0",
                                                                       "draw_od": "3.0", "away_od": "4.0"}]}}})))
    db.add_all(rows)
    db.commit()

    stats = sportsbook_empty_stats(db)
    assert stats["fanduel"]["calls"] == 5
    assert stats["fanduel"]["empty"] == 5
    assert stats["fanduel"]["empty_rate"] == 1.0
    assert stats["bet365"]["empty_rate"] == 0.0

    # Must not raise, and must surface a warning naming the empty sportsbook.
    health = data_health(db)
    assert any("fanduel" in w for w in health["warnings"])
    assert health["sportsbook_empty_stats"]["fanduel"]["calls"] == 5


def test_first_live_priority_ordering():
    now = datetime(2026, 1, 1, 12, 0, 0)

    def _m(mid, seconds_from_now):
        home = Player(id=mid * 10, name=f"H{mid}", league="L")
        away = Player(id=mid * 10 + 1, name=f"A{mid}", league="L")
        m = Match(id=mid, start_time=now + timedelta(seconds=seconds_from_now), league="L",
                  home_player_id=home.id, away_player_id=away.id, source="betsapi")
        return m

    live_missing = _m(1, -10)       # live, no first-live snapshot yet -> tier 0
    near_kickoff = _m(2, 20)        # 20s pre-kickoff -> tier 1
    two_min_out = _m(3, 100)        # 100s pre-kickoff -> tier 2
    distant_new = _m(4, 500)        # 500s out, never polled -> tier 4
    distant_tracked = _m(5, 500)    # 500s out, already polled before -> tier 3

    poller._LAST_POLLED.clear()
    poller._LAST_POLLED[distant_tracked.id] = now - timedelta(seconds=60)

    matches = [distant_new, two_min_out, distant_tracked, live_missing, near_kickoff]
    ordered = sorted(matches, key=lambda m: poller._match_priority(m, now, {live_missing.id}))

    assert [m.id for m in ordered] == [
        live_missing.id, near_kickoff.id, two_min_out.id, distant_tracked.id, distant_new.id,
    ]
    poller._LAST_POLLED.clear()


def test_validation_mode_cap_never_evicts_live_missing_first_live_match():
    """Regression for the 2026-07-06 max_matches=2 session bug: a match that
    went live minutes ago (large abs(seconds_to_kickoff)) must still win a
    slot over a match that merely hasn't kicked off yet (small abs(s2k)),
    when the tracked window is capped down to a handful of matches. Ranking
    by raw abs(s2k) got this backwards; ranking by _match_priority (tier 0 =
    live and missing first-live) does not."""
    now = datetime(2026, 1, 1, 12, 0, 0)

    def _m(mid, seconds_from_now):
        home = Player(id=mid * 10, name=f"H{mid}", league="L")
        away = Player(id=mid * 10 + 1, name=f"A{mid}", league="L")
        return Match(id=mid, start_time=now + timedelta(seconds=seconds_from_now), league="L",
                     home_player_id=home.id, away_player_id=away.id, source="betsapi")

    live_missing_stale = _m(1, -320)  # went live 5+ minutes ago, still no first-live snapshot
    almost_kickoff = _m(2, 60)        # kicks off in 1 minute -- numerically "closer" by abs(s2k)
    also_almost = _m(3, 90)

    window = [almost_kickoff, also_almost, live_missing_stale]
    cap = 2
    capped = sorted(window, key=lambda m: poller._match_priority(m, now, {live_missing_stale.id}))[:cap]

    assert live_missing_stale.id in {m.id for m in capped}, (
        "the live-and-missing-first-live match was evicted by a merely-imminent one")


def test_seed_data_and_sportsbooks_off_by_default():
    db = _db()
    s = db.get(Settings, 1)
    assert s.include_seed_data is False
    assert json.loads(s.sportsbooks_tracked) == ["bet365"]
    assert s.validation_mode_enabled is False
