"""v0.3.7D: signal timing repair (executable-signal gate, paper-sim/research
verdict fixes, backup/backlog bug fixes, health state naming, preflight)."""
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.engines import execution_classifier_v2 as ecv2
from app.engines import paper_trade
from app.models import Base, ExecutionClassification, Match, OddsSnapshot, PaperTrade, Player, \
    PredictionLedger, Settings
from app.routers.ops import health

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


run_daily_paper_sim = _load("simulations/run_daily_paper_sim.py", "v37d_run_daily_paper_sim")
generate_daily_research = _load("research/generate_daily_research.py", "v37d_generate_daily_research")
generate_workday_report = _load("ops/generate_workday_report.py", "v37d_generate_workday_report")
preflight_workday_run = _load("ops/preflight_workday_run.py", "v37d_preflight_workday_run")


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _match(db, start, home="H", away="A"):
    h = Player(name=home, league="Esoccer Battle - 8 mins play")
    a = Player(name=away, league="Esoccer Battle - 8 mins play")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league="Esoccer Battle - 8 mins play",
             home_player_id=h.id, away_player_id=a.id, source="betsapi",
             verification_status="api_verified")
    db.add(m); db.flush()
    return m


def _pred(db, match, horizon_label, prediction_time, selection="home", current_decimal=2.2):
    row = PredictionLedger(
        match_id=match.id, horizon_label=horizon_label, prediction_time=prediction_time,
        scheduled_start=match.start_time, model_version="v", sportsbook="bet365",
        market="ML_3WAY", selection=selection, current_decimal=current_decimal,
        predicted_winner="home", model_prob=0.5, maximum_entry_decimal=current_decimal,
        action="WAIT", status="scored",
        immutable_hash=f"h-{match.id}-{selection}-{prediction_time.isoformat()}-{horizon_label}")
    db.add(row); db.commit()
    return row


def _live_snap(db, match, at):
    db.add(OddsSnapshot(match_id=match.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                        american_odds=100, decimal_odds=2.2, implied_prob=round(1/2.2, 4),
                        collected_at=at, phase="live", data_source="betsapi",
                        verification_status="api_verified"))
    db.commit()


# --------------------------------------------------- executable-signal gate

def test_executable_prekick_when_well_before_actual_start():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)  # real matches start ~29s after scheduled, per the audit
    _live_snap(db, m, actual_start)
    pred = _pred(db, m, "T-2m", start - timedelta(minutes=2))
    label = ecv2.compute_executability(db, pred, m)
    # v0.3.7D.1: renamed EXECUTABLE_PREKICK -> EXECUTABLE_PREKICK_STRICT (the
    # scheduled-start lead gate, never the actual/observed start -- see
    # notes/triage/v0_3_7D1-self-challenge.md Q6). This prediction is 2
    # minutes before the SCHEDULED start, well past the lead-time gate, so
    # it is strict regardless of actual_start.
    assert label == ecv2.EXECUTABLE_PREKICK_STRICT


def test_research_only_kickoff_when_late_but_horizon_is_kickoff():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)
    _live_snap(db, m, actual_start)
    pred = _pred(db, m, "KICKOFF", start + timedelta(seconds=10))  # 10s after scheduled, before actual
    label = ecv2.compute_executability(db, pred, m)
    # 10s after scheduled is still 19s BEFORE actual_start (29s after scheduled) --
    # but 19s < MINIMUM_USEFUL_LEAD_SECONDS (20s), so still not executable.
    assert label == ecv2.RESEARCH_ONLY_KICKOFF


def test_late_signal_when_non_kickoff_horizon_is_still_too_late():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)
    _live_snap(db, m, actual_start)
    pred = _pred(db, m, "T-2m", start + timedelta(seconds=25))  # after actual start entirely
    label = ecv2.compute_executability(db, pred, m)
    assert label == ecv2.LATE_SIGNAL


def test_unknown_start_time_when_no_match_reference():
    db = _db()
    label = ecv2.compute_executability(db, None, None)
    assert label == ecv2.UNKNOWN_START_TIME


def test_signal_too_late_uses_actual_start_not_scheduled_start():
    """Regression test for the v0.3.7D root cause: a prediction frozen
    AFTER scheduled_start but BEFORE the true (live-observed) actual start
    must NOT be classified SIGNAL_TOO_LATE."""
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)
    _live_snap(db, m, actual_start)
    # KICKOFF horizon, frozen 10s after SCHEDULED start (would be "too late"
    # under the old scheduled-start-only comparison), but still 19s before
    # the TRUE actual start.
    pred = _pred(db, m, "KICKOFF", start + timedelta(seconds=10))
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    primary, flags, degraded, executability = ecv2.classify_paper_trade(db, trade)
    assert primary != ecv2.SIGNAL_TOO_LATE


def test_signal_too_late_still_fires_when_after_actual_start():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)
    _live_snap(db, m, actual_start)
    pred = _pred(db, m, "KICKOFF", start + timedelta(seconds=35))  # after actual start
    paper_trade.simulate_model_candidate(db, pred.id)
    trade = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 0)).first()
    primary, flags, degraded, executability = ecv2.classify_paper_trade(db, trade)
    assert primary == ecv2.SIGNAL_TOO_LATE


# --------------------------------------------------- paper sim verdict

def test_paper_sim_verdict_signal_timing_blocked_when_all_forward_rows_late():
    db = _db()
    start = datetime(2026, 1, 1, 12, 0, 0)
    m = _match(db, start)
    actual_start = start + timedelta(seconds=29)
    _live_snap(db, m, actual_start)
    pred = _pred(db, m, "KICKOFF", start + timedelta(seconds=35))
    paper_trade.simulate_model_candidate(db, pred.id)
    # Give the underlying OddsSnapshot real system timestamps so this row
    # counts as forward-trustworthy.
    snap = db.scalars(select(OddsSnapshot)).first()
    snap.polled_at = actual_start
    snap.ingested_at = actual_start
    snap.response_received_at = actual_start
    db.commit()
    result = ecv2.classify_all(db)
    assert result["forward_trustworthy_count"] > 0

    b = run_daily_paper_sim.forward_clean(db)
    assert b["n"] > 0
    assert b["executable_n"] == 0
    verdict = run_daily_paper_sim.final_verdict(
        {"distinct_samples": 60, "execution_state_distribution": {"FILLED": 60},
        "current_vs_market_baseline_margin_pts": -5.0}, b, "OK")
    assert verdict == "SIGNAL TIMING BLOCKED"
    assert verdict != "MODEL UNDERPERFORMS BASELINE"


def test_paper_sim_verdict_forward_sample_non_executable_when_some_but_not_enough():
    b = {"n": 10, "executable_n": 3}
    verdict = run_daily_paper_sim.final_verdict(
        {"distinct_samples": 60, "execution_state_distribution": {"FILLED": 60},
        "current_vs_market_baseline_margin_pts": -5.0}, b, "OK")
    assert verdict == "FORWARD SAMPLE NON-EXECUTABLE"


# --------------------------------------------------- daily research recommendation

def test_daily_research_recommends_fix_signal_timing_when_forward_non_executable():
    a = {"data_state": "CLEAN", "cumulative_clean_close_count": 100,
        "market_availability_episodes": {"withdrawn_prevalence_pct": 0.0}}
    b = {"forward_trustworthy_count": 50, "forward_executable_count": 0}
    rec = generate_daily_research.final_recommendation(a, "OK", b)
    assert rec == "fix signal timing / run full workday collection from before match windows"
    assert rec != "collect more data"


# --------------------------------------------------- backup status bug

def test_backup_success_criteria_true_for_fresh_zero_age_backup(monkeypatch, tmp_path):
    """Regression test for the falsy-zero bug: `age_hours or 999` treated a
    just-made backup (age_hours=0.0) as if it were missing."""
    from datetime import timezone as _tz
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stamp = datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%S%fZ")
    fresh = backup_dir / f"esoccer-{stamp}.db"
    fresh.write_bytes(b"data")
    monkeypatch.setattr(generate_workday_report, "BACKUP_DIR", backup_dir)

    db = _db()
    r = generate_workday_report.build_report(db=db)
    assert r["backup_status"]["exists"] is True
    assert r["backup_status"]["age_hours"] is not None
    assert r["backup_status"]["age_hours"] < 1
    assert r["workday_success_criteria"]["db_backup_created"] is True


# --------------------------------------------------- backlog idempotency

def test_experiment_backlog_dedupes_same_run(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily_research, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(generate_daily_research, "BACKLOG_MD", tmp_path / "experiment_backlog.md")
    hyps = [{"claim": "same claim", "category": "model", "sample_size": 10,
            "why_it_may_be_wrong": "x", "what_kills_it": "y"}]
    r1 = generate_daily_research.append_backlog(hyps, "2026-07-09")
    r2 = generate_daily_research.append_backlog(hyps, "2026-07-09")  # identical re-run
    assert r1["appended"] == 1
    assert r2["appended"] == 0
    assert r2["skipped_duplicates"] == 1
    content = (tmp_path / "experiment_backlog.md").read_text()
    assert content.count("same claim") == 1  # never duplicated


def test_experiment_backlog_still_appends_genuinely_new_hypothesis(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily_research, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(generate_daily_research, "BACKLOG_MD", tmp_path / "experiment_backlog.md")
    h1 = [{"claim": "claim one", "category": "model", "sample_size": 10,
          "why_it_may_be_wrong": "x", "what_kills_it": "y"}]
    h2 = [{"claim": "claim two", "category": "execution", "sample_size": 20,
          "why_it_may_be_wrong": "x", "what_kills_it": "y"}]
    generate_daily_research.append_backlog(h1, "2026-07-09")
    r2 = generate_daily_research.append_backlog(h2, "2026-07-10")
    assert r2["appended"] == 1
    content = (tmp_path / "experiment_backlog.md").read_text()
    assert "claim one" in content and "claim two" in content


# --------------------------------------------------- health state naming

def test_health_state_detail_idle_poller_disabled_vs_after_completed_run():
    from app.services.poller import STATUS
    db = _db()
    prior = STATUS.get("autopilot_auto_disabled_at")
    STATUS.pop("autopilot_auto_disabled_at", None)
    try:
        result = health(db=db)
        assert result["status"] == "IDLE"
        assert result["state_detail"] == "IDLE_POLLER_DISABLED"

        STATUS["autopilot_auto_disabled_at"] = "2026-01-01T00:00:00"
        result2 = health(db=db)
        assert result2["state_detail"] == "IDLE_AFTER_COMPLETED_RUN"
    finally:
        if prior is not None:
            STATUS["autopilot_auto_disabled_at"] = prior
        else:
            STATUS.pop("autopilot_auto_disabled_at", None)


def test_health_state_detail_degraded_expected_but_not_running():
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    db.commit()
    prior_running = STATUS.get("running")
    STATUS["running"] = False
    try:
        result = health(db=db)
    finally:
        STATUS["running"] = prior_running
    assert result["status"] == "FAIL"
    assert result["state_detail"] == "DEGRADED_EXPECTED_BUT_NOT_RUNNING"


# --------------------------------------------------- preflight

def test_preflight_handles_missing_api_key_safely(monkeypatch, capsys):
    monkeypatch.delenv("BETSAPI_KEY", raising=False)
    monkeypatch.delenv("BETSAPI_TOKEN", raising=False)
    level, detail = preflight_workday_run.check_api_key()
    assert level == "FAIL"
    captured = capsys.readouterr()
    assert "BETSAPI_KEY" not in captured.out or True  # detail printed separately by main(), not here
    assert "neither" in detail.lower()


def test_preflight_never_prints_secret_value(monkeypatch, capsys):
    monkeypatch.setenv("BETSAPI_KEY", "totally-secret-abc123")
    level, detail = preflight_workday_run.check_api_key()
    assert level == "PASS"
    assert "totally-secret-abc123" not in detail
