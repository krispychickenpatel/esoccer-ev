"""v0.3.7D.1 Task 8/12: daily recommendation awareness -- exact message
shape for each run-state case, and correct strict-executable CLV n/gate
reporting (no live DB; strict_forward_clv naturally returns n=0 on an
empty in-memory DB, which is all these tests need)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import daily_recommendation
from app.models import Base, Settings


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def test_active_run_recommends_continue():
    db = _db()
    health = {"status": "OK",
             "active_run": {"run_started_at": "2026-07-12T14:00:00", "configured_max_minutes": 480,
                            "actual_runtime_minutes": 30.0, "in_startup_grace": False},
             "last_completed_run": None}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["action"] == "CONTINUE_COLLECTION"
    assert "CONTINUE DAILY COLLECTION" in r["message"]
    assert "n=0/150" in r["message"]


def test_completed_run_below_gate_recommends_another_run():
    db = _db()
    health = {"status": "IDLE", "active_run": None,
             "last_completed_run": {"run_started_at": "2026-07-12T06:00:00",
                                    "run_completed_at": "2026-07-12T14:00:00",
                                    "configured_max_minutes": 480, "actual_runtime_minutes": 480.0}}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["action"] == "START_ANOTHER_RUN"
    assert "RUN COMPLETE, SAMPLE STILL BUILDING" in r["message"]
    assert "run_workday_autopilot.py" in r["message"]


def test_completed_run_at_gate_recommends_verdict_review(monkeypatch):
    from app.engines import strict_forward_metrics

    def fake_strict_forward_clv(db, lead_s, **kwargs):
        return {"strict_executable_forward_clv_n": 150}

    monkeypatch.setattr(strict_forward_metrics, "strict_forward_clv", fake_strict_forward_clv)
    db = _db()
    health = {"status": "IDLE", "active_run": None,
             "last_completed_run": {"run_started_at": "2026-07-12T06:00:00",
                                    "run_completed_at": "2026-07-12T14:00:00",
                                    "configured_max_minutes": 480, "actual_runtime_minutes": 480.0}}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["action"] == "REVIEW_VERDICT"
    assert "SAMPLE GATE MET" in r["message"]
    assert "n=150/150" in r["message"]


def test_no_run_recorded_recommends_first_run():
    db = _db()
    health = {"status": "IDLE", "active_run": None, "last_completed_run": None}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["action"] == "START_FIRST_RUN"
    assert "NO RUN RECORDED" in r["message"]
    assert "preflight_workday_run.py" in r["message"]
