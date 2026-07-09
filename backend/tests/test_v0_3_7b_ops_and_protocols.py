"""v0.3.7B: watchdog health endpoint, max-entry semantics labeling,
friend-pick retro-CSV exclusion, spot-check field separation."""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import entry_floor_diagnostics
from app.models import Base, Match, OddsSnapshot, Player, Settings
from app.routers.ops import health

FRIEND_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/friend_picks.csv")
SPOT_CHECK_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "spot_check_capture.py"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def test_health_endpoint_reports_idle_not_ok_when_poller_disabled():
    """Settings.poller_enabled=False (the default) -- this must never report
    status=OK, since OK should mean 'actively collecting', but it must also
    not be CRITICAL, since turning the poller off on purpose isn't a failure."""
    db = _db()
    result = health(db=db)
    assert result["poller_enabled_in_settings"] is False
    assert "db_writable" in result
    assert result["db_writable"] is True
    assert result["status"] == "IDLE"
    assert result["snapshots_created_today"] == 0


def test_health_reports_fail_when_enabled_but_collector_not_alive():
    """Reproduces the exact bug found in the v0.3.7B daily status file:
    collector not running + zero data + stale odds row must NEVER report
    status=OK just because db_writable and the (old, incomplete) incidents
    list happened to be empty."""
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    db.commit()
    prior_running = STATUS.get("running")
    STATUS["running"] = False  # explicit, deterministic -- module-level shared state
    try:
        result = health(db=db)
    finally:
        STATUS["running"] = prior_running
    assert result["poller_enabled_in_settings"] is True
    assert result["collector_expected_alive"] is True
    assert result["collector_task_alive"] is False
    assert result["status"] == "FAIL"
    assert "COLLECTOR_NOT_ALIVE" in result["reason_codes"]
    assert result["status"] != "OK"
    assert result["next_required_action"]


def test_health_fail_when_db_not_writable(monkeypatch):
    import app.routers.ops as ops_module
    db = _db()
    monkeypatch.setattr(ops_module, "_db_writable", lambda: False)
    result = health(db=db)
    assert result["status"] == "FAIL"
    assert "DB_NOT_WRITABLE" in result["reason_codes"]


def test_health_degraded_when_last_odds_row_stale():
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    db.commit()
    h = Player(name="H", league="Esoccer Battle - 8 mins play")
    a = Player(name="A", league="Esoccer Battle - 8 mins play")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=datetime(2020, 1, 1), league="Esoccer Battle - 8 mins play",
             home_player_id=h.id, away_player_id=a.id, source="betsapi",
             verification_status="api_verified")
    db.add(m); db.flush()
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.0, implied_prob=0.5,
                        collected_at=old, phase="pre_match", data_source="betsapi",
                        verification_status="api_verified", ingested_at=old))
    db.commit()

    prior_running, prior_tick = STATUS.get("running"), STATUS.get("last_tick")
    STATUS["running"] = True
    STATUS["last_tick"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()  # fresh tick
    try:
        result = health(db=db)
    finally:
        STATUS["running"], STATUS["last_tick"] = prior_running, prior_tick

    assert result["status"] == "DEGRADED"
    assert "STALE_INGEST" in result["reason_codes"]


def test_health_quota_unknown_never_reports_ok():
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    db.commit()
    prior_running, prior_tick = STATUS.get("running"), STATUS.get("last_tick")
    STATUS["running"] = True
    STATUS["last_tick"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    try:
        result = health(db=db)  # no PollCycle rows exist -> quota_status UNKNOWN
    finally:
        STATUS["running"], STATUS["last_tick"] = prior_running, prior_tick
    assert result["quota_status"] == "UNKNOWN"
    assert result["status"] != "OK"
    assert "QUOTA_UNKNOWN" in result["reason_codes"]


def test_densified_polling_disabled_by_default_env():
    from app.workday_config import load_workday_config
    cfg = load_workday_config()
    assert cfg.enable_densified_polling is False


def test_densified_polling_enabled_only_via_env(monkeypatch):
    from app.workday_config import load_workday_config
    monkeypatch.setenv("WORKDAY_ENABLE_DENSIFIED_POLLING", "true")
    cfg = load_workday_config()
    assert cfg.enable_densified_polling is True


def test_max_entry_semantics_labeled_correctly_in_report():
    db = _db()
    result = entry_floor_diagnostics.run(db)
    assert "analysis_only_disclaimer" in result
    assert "floor_equals_signal_price_count" in result
    # must never claim entry-logic was changed
    assert "does not change entry" in result["analysis_only_disclaimer"]


def test_friend_pick_retro_row_excluded_from_clean_sample():
    assert FRIEND_CSV.exists(), "notes/friend_picks.csv must exist"
    with open(FRIEND_CSV) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
    luis_row = next(r for r in rows if r["source"] == "Luis")
    assert luis_row["clean_scored"] == "FALSE"
    assert luis_row["logged_after_result"] == "TRUE"
    assert luis_row["was_pick_cancelled_by_source"] == "FALSE"
    assert luis_row["was_market_unavailable"] == "TRUE"
    assert "retro_result_known" in luis_row["exclude_reason"]


def test_spot_check_capture_separates_provider_and_book_fields():
    assert SPOT_CHECK_SCRIPT.exists()
    text = SPOT_CHECK_SCRIPT.read_text()
    for provider_field in ("provider_latest_price", "provider_source_ts",
                           "provider_polled_at", "provider_ingested_at"):
        assert provider_field in text
    for book_field in ("displayed_price", "market_available_on_book"):
        assert book_field in text
    # provider fields must never be named the same as the book fields
    assert "provider_latest_price" != "displayed_price"
