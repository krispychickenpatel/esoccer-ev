"""v0.3.7D.1 Task 8/12, updated for v0.3.7D.3: daily recommendation
awareness -- exact message shape for each run-state case, and correct
strict-executable CLV n/gate reporting.

v0.3.7D.3 replaced the old CONTINUE_COLLECTION/START_ANOTHER_RUN/
REVIEW_VERDICT/START_FIRST_RUN action taxonomy (derived directly from raw
health['active_run']/health['last_completed_run']) with actions derived from
the shared collection_evidence resolver and verdict_hierarchy branch that
fired -- see daily_recommendation.py and
notes/triage/v0_3_7D3-recommendation-consistency.md. These tests cover the
new taxonomy; the run-state/evidence-source semantics they replace are
covered in more detail by test_v0_3_7d3_recommendation_consistency.py."""
from datetime import datetime

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


def test_active_run_recommends_continue_daily_collection():
    db = _db()
    health = {"status": "OK",
             "active_run": {"run_started_at": "2026-07-12T14:00:00", "configured_max_minutes": 480,
                            "actual_runtime_minutes": 30.0, "in_startup_grace": False},
             "last_completed_run": None, "expected_collection_window_active": True}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["collection_has_run"] is True
    assert r["collection_evidence_source"] == "ACTIVE_RUN"
    # empty in-memory DB -> strict CLV n=0 -> below the directional gate
    assert r["action"] == "CONTINUE_DAILY_COLLECTION"
    assert "n=0/50" in r["message"]


def test_no_qualifying_evidence_recommends_start_collection():
    db = _db()
    health = {"status": "IDLE", "active_run": None, "last_completed_run": None,
             "expected_collection_window_active": True}
    r = daily_recommendation.build_recommendation(db, health)
    assert r["collection_has_run"] is False
    assert r["action"] == "START_COLLECTION"
    assert "no qualifying collection evidence" in r["message"]


def test_completed_run_in_window_below_directional_gate_continues_daily_collection():
    db = _db()
    health = {"status": "IDLE", "active_run": None,
             "last_completed_run": {"run_started_at": "2026-07-12T06:00:00",
                                    "run_completed_at": "2026-07-12T14:00:00",
                                    "configured_max_minutes": 480, "actual_runtime_minutes": 480.0},
             "expected_collection_window_active": True}
    r = daily_recommendation.build_recommendation(
        db, health, now=datetime(2026, 7, 12, 15, 0, 0))
    assert r["collection_has_run"] is True
    assert r["collection_evidence_source"] == "COMPLETED_RUN_METADATA"
    assert r["action"] == "CONTINUE_DAILY_COLLECTION"
    assert "n=0/50" in r["message"]


def test_decision_grade_gate_reached_reviews_model(monkeypatch):
    from app.engines import strict_forward_metrics

    def fake_strict_forward_clv(db, lead_s, **kwargs):
        return {"strict_executable_forward_clv_n": 150, "avg_decimal_clv_pct": -1.0}

    def fake_cross_tab(db):
        return {"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 150},
               "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 150}}}

    def fake_paired(db, lead_s=20.0):
        return {"scored_n": 150, "significant_baseline_outperformance": False}

    monkeypatch.setattr(strict_forward_metrics, "strict_forward_clv", fake_strict_forward_clv)
    monkeypatch.setattr(strict_forward_metrics, "forward_executability_primary_state_cross_tab", fake_cross_tab)
    monkeypatch.setattr(strict_forward_metrics, "paired_market_baseline_comparison", fake_paired)

    db = _db()
    health = {"status": "IDLE", "active_run": None,
             "last_completed_run": {"run_started_at": "2026-07-12T06:00:00",
                                    "run_completed_at": "2026-07-12T14:00:00",
                                    "configured_max_minutes": 480, "actual_runtime_minutes": 480.0},
             "expected_collection_window_active": True}
    r = daily_recommendation.build_recommendation(
        db, health, now=datetime(2026, 7, 12, 15, 0, 0))
    assert r["action"] == "REVIEW_MODEL_KILL_OR_REBUILD"
    assert r["verdict_branch"] == "MODEL_NEGATIVE_CLV"
