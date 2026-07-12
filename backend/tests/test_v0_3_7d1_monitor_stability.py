"""v0.3.7D.1 Task 10/12: monitor and health stability -- startup grace,
midnight-rollover-safe activity counters, and durable
IDLE_AFTER_COMPLETED_RUN reporting (survives a backend restart, and does
not stick around forever after a later manual disable)."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Match, OddsSnapshot, Player, Settings
from app.routers.ops import health


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def test_startup_grace_prevents_false_fail_right_after_autopilot_start():
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    s.autopilot_started_at = datetime.utcnow()  # just started
    s.autopilot_max_runtime_minutes = 480
    db.commit()
    prior_running = STATUS.get("running")
    STATUS["running"] = False  # collector hasn't ticked yet -- expected during grace
    try:
        result = health(db=db)
    finally:
        STATUS["running"] = prior_running
    assert result["status"] == "STARTING"
    assert result["state_detail"] == "AUTOPILOT_STARTUP_GRACE"
    assert result["status"] != "FAIL"
    assert result["active_run"]["run_started_at"] is not None
    assert result["active_run"]["in_startup_grace"] is True


def test_fail_still_fires_after_grace_period_elapses_with_zero_activity():
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    s.autopilot_started_at = datetime.utcnow() - timedelta(seconds=600)  # long past the 180s default grace
    s.autopilot_max_runtime_minutes = 480
    db.commit()
    prior_running = STATUS.get("running")
    STATUS["running"] = False
    try:
        result = health(db=db)
    finally:
        STATUS["running"] = prior_running
    assert result["status"] == "FAIL"
    assert "COLLECTOR_NOT_ALIVE" in result["reason_codes"]


def test_midnight_rollover_uses_run_start_not_calendar_today(monkeypatch):
    """Reproduces the exact false alarm this release fixes: an autopilot run
    started yesterday evening, still active, and 'now' is just after
    midnight -- calendar-date counters would read zero even though the run
    has snapshots from hours earlier in the run."""
    from app.services.poller import STATUS
    db = _db()

    h1 = Player(name="H", league="L")
    a1 = Player(name="A", league="L")
    db.add_all([h1, a1]); db.flush()
    m = Match(start_time=datetime(2026, 7, 11, 20, 0, 0), league="L",
             home_player_id=h1.id, away_player_id=a1.id, source="betsapi",
             verification_status="api_verified")
    db.add(m); db.flush()

    run_started_at = datetime(2026, 7, 11, 18, 0, 0)  # yesterday evening
    snap_time = datetime(2026, 7, 11, 23, 0, 0)  # also yesterday, well before midnight
    db.add(OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.0, implied_prob=0.5,
                        collected_at=snap_time, ingested_at=snap_time, polled_at=snap_time,
                        phase="pre", data_source="betsapi", verification_status="api_verified"))
    s = db.get(Settings, 1)
    s.poller_enabled = True
    s.autopilot_started_at = run_started_at
    s.autopilot_max_runtime_minutes = 480
    db.commit()

    prior_running = STATUS.get("running")
    prior_tick = STATUS.get("last_tick")
    STATUS["running"] = True
    just_after_midnight = datetime(2026, 7, 12, 0, 0, 35)
    STATUS["last_tick"] = (just_after_midnight - timedelta(seconds=5)).isoformat()

    import app.routers.ops as ops_module
    real_datetime = ops_module.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return just_after_midnight.replace(tzinfo=tz) if tz else just_after_midnight

    monkeypatch.setattr(ops_module, "datetime", _FrozenDatetime)
    try:
        result = health(db=db)
    finally:
        STATUS["running"] = prior_running
        STATUS["last_tick"] = prior_tick

    assert result["activity_window_kind"] == "since_run_start"
    assert result["snapshots_created_today"] == 1  # NOT zero -- the midnight-rollover bug this fixes
    assert "NO_SNAPSHOTS_TODAY" not in result["reason_codes"]


def test_idle_after_completed_run_uses_persisted_fields_when_recent():
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = False
    s.last_completed_run_started_at = datetime.utcnow() - timedelta(hours=2)
    s.last_completed_run_completed_at = datetime.utcnow() - timedelta(minutes=5)
    s.last_completed_run_max_minutes = 480
    db.commit()
    result = health(db=db)
    assert result["state_detail"] == "IDLE_AFTER_COMPLETED_RUN"
    assert result["last_completed_run"]["configured_max_minutes"] == 480
    assert result["last_completed_run"]["actual_runtime_minutes"] is not None


def test_idle_poller_disabled_when_completed_run_is_stale():
    """A completed run from long ago must not permanently masquerade as
    'just completed' after a later, unrelated manual disable."""
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = False
    s.last_completed_run_started_at = datetime.utcnow() - timedelta(days=3)
    s.last_completed_run_completed_at = datetime.utcnow() - timedelta(days=3)
    s.last_completed_run_max_minutes = 480
    db.commit()
    result = health(db=db)
    assert result["state_detail"] == "IDLE_POLLER_DISABLED"
