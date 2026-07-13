"""v0.3.7D.2: regression coverage for the two daily-cycle integration
defects found immediately after the v0.3.7D.1 merge:

1. scripts/research/generate_daily_research.py referenced the removed
   execution_classifier_v2.EXECUTABLE_PREKICK constant (renamed to
   EXECUTABLE_PREKICK_STRICT in D.1) -- crashed only when forward,
   non-degraded ExecutionClassification rows actually exist, which is why
   it slipped past the D.1 test suite (every existing call to
   section_b_execution_learning() in this repo uses an empty database).

2. verdict_hierarchy's COLLECTION_NOT_RUN branch relied solely on the new
   Settings.last_completed_run_* bookkeeping, which is NULL for any run
   that completed under pre-D.1 code -- collection_evidence.py fixes this
   with a migration-safe, time-bounded fallback.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import collection_evidence, verdict_hierarchy
from app.models import Base, Match, OddsSnapshot, PaperTrade, Player, PredictionLedger, Settings

REPO_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_DIR / "backend"
SCRIPTS_DIR = REPO_DIR / "scripts"


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


generate_daily_research = _load("research/generate_daily_research.py", "v37d2_generate_daily_research")


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _seed_one_forward_trustworthy_trade(db, now: datetime):
    h = Player(name="H", league="L"); a = Player(name="A", league="L")
    db.add_all([h, a]); db.flush()
    start = now - timedelta(hours=1)
    m = Match(start_time=start, league="L", home_player_id=h.id, away_player_id=a.id,
             source="betsapi", verification_status="api_verified")
    db.add(m); db.flush()
    pred = PredictionLedger(
        match_id=m.id, horizon_label="T-5m", prediction_time=start - timedelta(minutes=5),
        scheduled_start=start, model_version="v", sportsbook="bet365", market="ML_3WAY",
        selection="home", current_decimal=2.2, predicted_winner="home", model_prob=0.5,
        maximum_entry_decimal=2.2, action="WAIT", status="scored",
        immutable_hash=f"h-{m.id}-home-{(start - timedelta(minutes=5)).isoformat()}")
    db.add(pred); db.flush()
    snap = OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                       american_odds=100, decimal_odds=2.2, implied_prob=round(1 / 2.2, 4),
                       collected_at=start - timedelta(minutes=5), phase="pre", data_source="betsapi",
                       verification_status="api_verified",
                       polled_at=start - timedelta(minutes=5), ingested_at=start - timedelta(minutes=5),
                       response_received_at=start - timedelta(minutes=5))
    db.add(snap); db.flush()
    trade = PaperTrade(match_id=m.id, signal_id=pred.id, signal_source="MODEL", delay_seconds=30,
                       selection="home", settlement_status="FILLED",
                       created_at=start - timedelta(minutes=5), signal_time=start - timedelta(minutes=5),
                       market="ML_3WAY", sportsbook="bet365", price_snapshot_id=snap.id,
                       price_decimal=2.2)
    db.add(trade); db.commit()
    return trade


# --------------------------------------------------- 1/2: stale constant

def test_generate_daily_research_executes_without_stale_constant_error():
    """This must use a NON-EMPTY forward-trustworthy dataset -- an empty DB
    never enters the loop body that reads the constant, and would pass even
    with the bug still present (exactly how it slipped through D.1)."""
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    _seed_one_forward_trustworthy_trade(db, now)
    result = generate_daily_research.section_b_execution_learning(db)
    assert "forward_executable_count" in result
    assert result["forward_trustworthy_count"] >= 1


def test_no_stale_execute_prekick_constant_references_remain():
    import re
    pattern = re.compile(r"execution_classifier_v2\.EXECUTABLE_PREKICK\b(?!_STRICT)")
    offenders = []
    for base in (BACKEND_DIR / "app", SCRIPTS_DIR):
        for path in base.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text()
            if pattern.search(text):
                offenders.append(str(path))
    assert offenders == [], f"stale EXECUTABLE_PREKICK reference(s) found: {offenders}"


# --------------------------------------------------- 3: migration-boundary completed run

def test_migration_boundary_run_is_not_treated_as_no_run():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    health = {
        "active_run": None, "last_completed_run": None,  # D.1 bookkeeping absent (pre-D.1 run)
        "poller_enabled_in_settings": False,
        "last_successful_poll_at": (now - timedelta(hours=2)).isoformat(),
        "last_successful_ingest_at": (now - timedelta(hours=2)).isoformat(),
        "last_availability_heartbeat_at": (now - timedelta(hours=2)).isoformat(),
        "expected_collection_window_active": True,
    }
    evidence = collection_evidence.resolve_collection_evidence(db, health, now)
    assert evidence["collection_has_run"] is True
    assert evidence["evidence_source"] == collection_evidence.LEGACY_RECENT_ACTIVITY_INFERRED

    cross_tab = {"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 107},
                "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 107}}}
    strict_clv = {"strict_executable_forward_clv_n": 41, "avg_decimal_clv_pct": -1.919}
    paired = {"scored_n": 41, "significant_baseline_outperformance": False}
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=evidence["collection_has_run"], active_collection_window=True,
        cross_tab=cross_tab, strict_clv=strict_clv, paired=paired)
    assert verdict["verdict"] == "FORWARD_CLV_INSUFFICIENT"


# --------------------------------------------------- 4: midnight-crossing

def test_midnight_crossing_activity_is_detected():
    db = _db()
    just_after_midnight = datetime(2026, 7, 13, 0, 10, 0)
    before_midnight = datetime(2026, 7, 12, 23, 50, 0)
    health = {
        "active_run": None, "last_completed_run": None,
        "poller_enabled_in_settings": False,
        "last_successful_poll_at": before_midnight.isoformat(),
        "last_successful_ingest_at": before_midnight.isoformat(),
        "last_availability_heartbeat_at": None,
        "expected_collection_window_active": True,
    }
    evidence = collection_evidence.resolve_collection_evidence(db, health, just_after_midnight)
    assert evidence["collection_has_run"] is True
    assert evidence["evidence_source"] == collection_evidence.LEGACY_RECENT_ACTIVITY_INFERRED


# --------------------------------------------------- 5/6: genuine no-run

def test_genuine_no_run_with_only_old_all_time_data_stays_collection_not_run():
    db = _db()
    now = datetime(2026, 7, 13, 2, 0, 0)
    long_ago = now - timedelta(days=10)
    _seed_one_forward_trustworthy_trade(db, long_ago)  # real, old forward-clean data exists
    health = {
        "active_run": None, "last_completed_run": None,
        "poller_enabled_in_settings": False,
        "last_successful_poll_at": long_ago.isoformat(),
        "last_successful_ingest_at": long_ago.isoformat(),
        "last_availability_heartbeat_at": None,
        "expected_collection_window_active": True,
    }
    evidence = collection_evidence.resolve_collection_evidence(db, health, now)
    assert evidence["collection_has_run"] is False
    assert evidence["evidence_source"] == collection_evidence.NO_EVIDENCE

    # all-time forward_clean_n is large/nonzero, but must NOT suppress COLLECTION_NOT_RUN
    cross_tab = {"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 5000},
                "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 5000}}}
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=evidence["collection_has_run"], active_collection_window=True,
        cross_tab=cross_tab, strict_clv={"strict_executable_forward_clv_n": 5000, "avg_decimal_clv_pct": 1.0},
        paired={"scored_n": 5000, "significant_baseline_outperformance": False})
    assert verdict["verdict"] == "COLLECTION_NOT_RUN"


def test_all_time_forward_clean_n_alone_cannot_suppress_collection_not_run():
    """Direct contract test on determine_verdict: a huge forward_clean_n in
    the cross-tab must never substitute for real collection-has-run
    evidence -- Branch 1 only ever looks at `collection_has_run`."""
    cross_tab = {"status": "OK", "row_totals": {"EXECUTABLE_PREKICK_STRICT": 999999},
                "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"FILLED": 999999}}}
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=False, active_collection_window=True,
        cross_tab=cross_tab, strict_clv={"strict_executable_forward_clv_n": 999999, "avg_decimal_clv_pct": 2.0},
        paired={"scored_n": 999999, "significant_baseline_outperformance": False})
    assert verdict["verdict"] == "COLLECTION_NOT_RUN"


# --------------------------------------------------- 7: full daily-cycle subprocess smoke test

def test_daily_cycle_subprocess_against_copied_temp_database():
    """Runs the REAL scripts/ops/run_daily_cycle.py entrypoint end-to-end
    against a disposable COPY of the latest verified backup -- never the
    live database. Several of the chained scripts (backup_db.py in
    particular) don't honor DATABASE_URL at all and always resolve
    `<repo>/backend/esoccer.db` by cwd -- so the copy is placed there
    (this worktree's own backend/esoccer.db, not the main repo's), and
    restored to its original (empty placeholder) state afterward. The
    report-writing scripts in this chain also share a few hardcoded
    absolute output paths under notes/ -- those are snapshotted and
    restored too, so this test can never leave a permanent side effect on
    shared real report state. Skips gracefully if no backup exists in this
    environment (backups/ is real, environment-specific state, not a
    repo-tracked fixture).

    Timeout is generous (10 minutes): re-classifying the full real dataset
    (~14k MODEL paper trades) via execution_classifier_v2.classify_all()
    and recomputing the strict-forward cross-tab/CLV waterfall over ~4.8k
    forward rows are both pre-existing, per-row-query-heavy operations
    (unchanged by this hotfix) -- measured at ~85s and ~110s respectively
    against the real verified backup on this machine."""
    import shutil

    live_backup_dir = REPO_DIR.parent / "esoccer-ev" / "backend" / "backups"
    candidates = sorted(live_backup_dir.glob("esoccer-*.db")) if live_backup_dir.exists() else []
    if not candidates:
        pytest.skip("no verified backup available in this environment")
    latest_backup = candidates[-1]

    worktree_db_path = BACKEND_DIR / "esoccer.db"
    worktree_backups_dir = BACKEND_DIR / "backups"
    notes_dirs = [Path("/Users/krispatell/Downloads/ESoccer/notes/status"),
                 Path("/Users/krispatell/Downloads/ESoccer/notes/research"),
                 Path("/Users/krispatell/Downloads/ESoccer/notes/simulations")]

    holding = tempfile.mkdtemp(prefix="v037d2_notes_snapshot_")
    snapshots = {}
    for d in notes_dirs:
        if d.exists():
            dest = Path(holding) / d.name
            shutil.copytree(d, dest)
            snapshots[d] = dest

    original_db_bytes = worktree_db_path.read_bytes() if worktree_db_path.exists() else None
    existing_backup_names = set(worktree_backups_dir.glob("*.db")) if worktree_backups_dir.exists() else set()

    try:
        shutil.copy2(latest_backup, worktree_db_path)

        migrate = subprocess.run(
            [sys.executable, "-c", "from app.database import init_db; init_db()"],
            cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=60)
        assert migrate.returncode == 0, migrate.stdout + migrate.stderr

        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ops" / "run_daily_cycle.py"),
            "--non-interactive", "--allow-warn"],
            cwd=str(REPO_DIR), capture_output=True, text=True, timeout=600)
        combined = result.stdout + result.stderr
        assert "FAILED" not in combined, combined
        assert result.returncode == 0, combined

        # v0.3.7D.3: the paper-sim step's evidence_consistency check must
        # agree -- verdict and daily recommendation are derived from the
        # same collection_evidence result on this real, reconciled data.
        assert "RECOMMENDATION_EVIDENCE_MISMATCH" not in combined, combined
        sim_json = json.loads((Path("/Users/krispatell/Downloads/ESoccer/notes/simulations")
                              / "latest_paper_sim.json").read_text())
        assert sim_json["evidence_consistency"]["consistent"] is True, sim_json["evidence_consistency"]
    finally:
        if original_db_bytes is not None:
            worktree_db_path.write_bytes(original_db_bytes)
        elif worktree_db_path.exists():
            worktree_db_path.unlink()
        if worktree_backups_dir.exists():
            for p in worktree_backups_dir.glob("*.db"):
                if p not in existing_backup_names:
                    p.unlink()
        for d, snap in snapshots.items():
            shutil.rmtree(d)
            shutil.copytree(snap, d)
        shutil.rmtree(holding, ignore_errors=True)
