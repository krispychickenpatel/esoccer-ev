"""v0.3.7D.3: daily_recommendation.py no longer derives run status directly
from health['active_run']/health['last_completed_run'] -- it now resolves
collection_evidence.resolve_collection_evidence() exactly once (shared with
verdict_hierarchy) and, once collection is known to have run, derives its
action from verdict_hierarchy.determine_verdict() itself. This fixes the
confirmed defect where the same daily report could say
verdict=FORWARD_CLV_INSUFFICIENT (valid recent collection evidence) next to
recommendation=NO RUN RECORDED / START_FIRST_RUN (raw last_completed_run
still NULL)."""
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import collection_evidence, daily_recommendation, verdict_hierarchy
from app.models import Base, Settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENGINE_SRC = BACKEND_DIR / "app" / "engines" / "daily_recommendation.py"

OK_CROSS_TAB = {"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 41},
               "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 41}}}
OK_PAIRED = {"scored_n": 41, "significant_baseline_outperformance": False}


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _build_both(db, health, now, cross_tab=None, strict_clv=None, paired=None):
    evidence = collection_evidence.resolve_collection_evidence(db, health, now)
    active_window = bool(health.get("expected_collection_window_active", True))
    cross_tab = cross_tab or OK_CROSS_TAB
    strict_clv = strict_clv or {"strict_executable_forward_clv_n": 41, "avg_decimal_clv_pct": -1.919}
    paired = paired or OK_PAIRED
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=evidence["collection_has_run"], active_collection_window=active_window,
        cross_tab=cross_tab, strict_clv=strict_clv, paired=paired)
    verdict["collection_has_run"] = evidence["collection_has_run"]
    verdict["collection_run_evidence_source"] = evidence["evidence_source"]
    recommendation = daily_recommendation.build_recommendation(
        db, health, now=now, evidence=evidence, cross_tab=cross_tab, strict_clv=strict_clv, paired=paired)
    return evidence, verdict, recommendation


# --------------------------------------------------- 1: migration-boundary

def test_migration_boundary_recommendation_is_not_start_first_run():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    health = {
        "active_run": None, "last_completed_run": None,  # no D.1 completed-run metadata
        "last_successful_poll_at": (now - timedelta(hours=2)).isoformat(),
        "last_successful_ingest_at": (now - timedelta(hours=2)).isoformat(),
        "last_availability_heartbeat_at": (now - timedelta(hours=2)).isoformat(),
        "expected_collection_window_active": True,
    }
    evidence, verdict, recommendation = _build_both(db, health, now)
    assert evidence["evidence_source"] == collection_evidence.LEGACY_RECENT_ACTIVITY_INFERRED
    assert verdict["verdict"] != "COLLECTION_NOT_RUN"
    assert recommendation["action"] not in ("START_FIRST_RUN", "NO_RUN_RECORDED", "START_COLLECTION")
    assert recommendation["collection_has_run"] is True


# --------------------------------------------------- 2: midnight-crossing

def test_midnight_crossing_verdict_and_recommendation_agree():
    db = _db()
    just_after_midnight = datetime(2026, 7, 13, 0, 10, 0)
    before_midnight = datetime(2026, 7, 12, 23, 50, 0)
    health = {
        "active_run": None, "last_completed_run": None,
        "last_successful_poll_at": before_midnight.isoformat(),
        "last_successful_ingest_at": before_midnight.isoformat(),
        "last_availability_heartbeat_at": None,
        "expected_collection_window_active": True,
    }
    evidence, verdict, recommendation = _build_both(db, health, just_after_midnight)
    assert evidence["collection_has_run"] is True
    assert verdict["collection_has_run"] == recommendation["collection_has_run"]
    assert verdict["collection_run_evidence_source"] == recommendation["collection_evidence_source"]


# --------------------------------------------------- 3: genuine no-run

def test_genuine_no_run_recommends_start_collection():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    long_ago = now - timedelta(days=10)
    health = {
        "active_run": None, "last_completed_run": None,
        "last_successful_poll_at": long_ago.isoformat(),
        "last_successful_ingest_at": long_ago.isoformat(),
        "last_availability_heartbeat_at": None,
        "expected_collection_window_active": True,
    }
    # all-time forward data exists (large n) but is NOT recent -- must not suppress either signal
    evidence, verdict, recommendation = _build_both(
        db, health, now,
        cross_tab={"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 5000},
                  "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 5000}}},
        strict_clv={"strict_executable_forward_clv_n": 5000, "avg_decimal_clv_pct": 1.0},
        paired={"scored_n": 5000, "significant_baseline_outperformance": False})
    assert evidence["collection_has_run"] is False
    assert verdict["verdict"] == "COLLECTION_NOT_RUN"
    assert recommendation["action"] == "START_COLLECTION"


# --------------------------------------------------- 4: active run

def test_active_run_recommendation_recognizes_current_collection():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    health = {
        "active_run": {"run_started_at": (now - timedelta(minutes=30)).isoformat(),
                       "configured_max_minutes": 480, "actual_runtime_minutes": 30.0,
                       "in_startup_grace": False},
        "last_completed_run": None,
        "expected_collection_window_active": True,
    }
    evidence, verdict, recommendation = _build_both(db, health, now)
    assert evidence["evidence_source"] == collection_evidence.ACTIVE_RUN
    assert recommendation["collection_has_run"] is True
    assert recommendation["action"] != "START_COLLECTION"


# --------------------------------------------------- 5: D.2 real-data shape

def test_real_data_shape_n_below_50_continues_daily_collection():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    health = {
        "active_run": None, "last_completed_run": None,
        "last_successful_poll_at": (now - timedelta(hours=1)).isoformat(),
        "last_successful_ingest_at": (now - timedelta(hours=1)).isoformat(),
        "last_availability_heartbeat_at": (now - timedelta(hours=1)).isoformat(),
        "expected_collection_window_active": True,
    }
    evidence, verdict, recommendation = _build_both(
        db, health, now,
        cross_tab={"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 107},
                  "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 107}}},
        strict_clv={"strict_executable_forward_clv_n": 41, "avg_decimal_clv_pct": -1.919},
        paired={"scored_n": 41, "significant_baseline_outperformance": False})
    assert verdict["verdict"] == "FORWARD_CLV_INSUFFICIENT"
    assert recommendation["action"] == "CONTINUE_DAILY_COLLECTION"
    assert "n=41/50" in recommendation["message"]
    assert recommendation["collection_evidence_source"] == evidence["evidence_source"]


# --------------------------------------------------- 6: evidence mismatch protection

def test_evidence_mismatch_is_detected():
    verdict = {"collection_has_run": True, "collection_run_evidence_source": "ACTIVE_RUN"}
    recommendation = {"collection_has_run": False, "collection_evidence_source": "NO_EVIDENCE"}
    result = daily_recommendation.check_evidence_consistency(verdict, recommendation)
    assert result["consistent"] is False
    assert result["flag"] == "RECOMMENDATION_EVIDENCE_MISMATCH"


def test_evidence_agreement_reports_consistent():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    health = {"active_run": None, "last_completed_run": None,
             "expected_collection_window_active": True}
    evidence, verdict, recommendation = _build_both(db, health, now)
    result = daily_recommendation.check_evidence_consistency(verdict, recommendation)
    assert result["consistent"] is True
    assert result["flag"] is None


# --------------------------------------------------- 7: no bypass of the resolver

def test_daily_recommendation_has_no_direct_active_run_bypass():
    """The confirmed D.3 defect: reading health['active_run'] or
    health['last_completed_run'] to derive collection_has_run/the run-state
    branch directly, bypassing collection_evidence.resolve_collection_evidence().
    Reading them for message/display metadata only (not decisioning) is fine
    and still present in `base` -- this test targets the decisioning path
    specifically: no `if ... health.get("active_run")` / `if ... health["active_run"]`
    conditional exists outside of pass-through dict construction."""
    src = ENGINE_SRC.read_text()
    assert "collection_evidence.resolve_collection_evidence" in src
    forbidden = re.compile(r'if\s+.*health(\.get\(["\']active_run["\']\)|\[["\']active_run["\']\])')
    assert not forbidden.search(src), "found a conditional branching directly on health['active_run']"
    forbidden_completed = re.compile(r'if\s+.*health(\.get\(["\']last_completed_run["\']\)|\[["\']last_completed_run["\']\])')
    assert not forbidden_completed.search(src), "found a conditional branching directly on health['last_completed_run']"
