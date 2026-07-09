"""v0.3.7C: Workday Autopilot, Daily Research Loop, Auto Paper Simulation.

Imports the scripts/ modules directly (they're plain Python files, not a
package) via sys.path, same convention the scripts themselves use to reach
the backend app package.
"""
import csv
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Match, OddsSnapshot, Player, PredictionLedger, Settings

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


backup_db = _load("ops/backup_db.py", "v37c_backup_db")
autopilot_status = _load("ops/autopilot_status.py", "v37c_autopilot_status")
generate_workday_report = _load("ops/generate_workday_report.py", "v37c_generate_workday_report")
generate_daily_research = _load("research/generate_daily_research.py", "v37c_generate_daily_research")
run_daily_paper_sim = _load("simulations/run_daily_paper_sim.py", "v37c_run_daily_paper_sim")


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _pred(db, match, selection="home", current_decimal=2.2, max_entry=None):
    pt = match.start_time - timedelta(minutes=5)
    row = PredictionLedger(
        match_id=match.id, horizon_label="T-5m", prediction_time=pt,
        scheduled_start=match.start_time, model_version="v", sportsbook="bet365",
        market="ML_3WAY", selection=selection, current_decimal=current_decimal,
        predicted_winner="home", model_prob=0.5,
        maximum_entry_decimal=max_entry if max_entry is not None else current_decimal,
        action="WAIT", status="scored",
        immutable_hash=f"h-{match.id}-{selection}-{pt.isoformat()}")
    db.add(row); db.commit()
    return row


def _match(db, start, home="H", away="A"):
    h = Player(name=home, league="Esoccer Battle - 8 mins play")
    a = Player(name=away, league="Esoccer Battle - 8 mins play")
    db.add_all([h, a]); db.flush()
    m = Match(start_time=start, league="Esoccer Battle - 8 mins play",
             home_player_id=h.id, away_player_id=a.id, source="betsapi",
             verification_status="api_verified")
    db.add(m); db.flush()
    return m


# ------------------------------------------------------- 2/3: workday report

def _force_no_reachable_backend(monkeypatch):
    """These report generators prefer the real backend's HTTP endpoint over
    an in-process health() call (see the v0.3.7C bug fix note in
    run_workday_autopilot.py) -- but that means a test's constructed
    in-memory db scenario would otherwise be bypassed by whatever a REAL
    backend happens to be reporting on this machine. Force the HTTP attempt
    to fail so the test exercises the in-process fallback path, which is
    what these tests are actually verifying."""
    import httpx
    def _raise(*a, **k):
        raise httpx.ConnectError("forced-for-test")
    monkeypatch.setattr(httpx, "get", _raise)


def test_workday_report_handles_empty_day(monkeypatch):
    _force_no_reachable_backend(monkeypatch)
    db = _db()
    r = generate_workday_report.build_report(db=db)
    assert r["odds_rows_collected_today"] == 0
    assert r["health"]["status"] == "IDLE"


def test_workday_report_handles_degraded_collector(monkeypatch):
    _force_no_reachable_backend(monkeypatch)
    from app.services.poller import STATUS
    db = _db()
    s = db.get(Settings, 1)
    s.poller_enabled = True
    db.commit()
    prior_running = STATUS.get("running")
    STATUS["running"] = False
    try:
        r = generate_workday_report.build_report(db=db)
    finally:
        STATUS["running"] = prior_running
    assert r["health"]["status"] == "FAIL"
    assert r["workday_success_criteria"]["clean_system_timestamps_on_new_rows"] is False


# ------------------------------------------------------- 4: backup

def test_backup_creates_valid_file(tmp_path):
    db_path = tmp_path / "esoccer.db"
    db_path.write_bytes(b"fake db bytes")
    out_dir = tmp_path / "backups"
    result = backup_db.run_backup(db_path, out_dir, keep=5)
    assert result["ok"] is True
    assert Path(result["path"]).exists()
    assert result["size_bytes"] > 0


def test_backup_missing_db_fails_safely(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    result = backup_db.run_backup(missing, tmp_path / "backups", keep=5)
    assert result["ok"] is False
    assert "error" in result


def test_backup_retention_only_deletes_own_backup_dir(tmp_path):
    db_path = tmp_path / "esoccer.db"
    db_path.write_bytes(b"data")
    out_dir = tmp_path / "backups"
    unrelated = out_dir
    for _ in range(5):
        backup_db.run_backup(db_path, out_dir, keep=2)
    remaining = list(out_dir.glob("esoccer-*.db"))
    assert len(remaining) == 2
    # nothing outside out_dir was touched
    assert db_path.exists()


# ------------------------------------------------------- 5/6: env validation, no secrets

def test_missing_api_key_fails_safely(monkeypatch):
    run_workday_autopilot = _load("ops/run_workday_autopilot.py", "v37c_run_workday_autopilot_a")
    monkeypatch.delenv("BETSAPI_KEY", raising=False)
    monkeypatch.delenv("BETSAPI_TOKEN", raising=False)
    problems = run_workday_autopilot.validate_env()
    assert len(problems) == 1
    assert "BETSAPI_KEY" in problems[0] or "BETSAPI_TOKEN" in problems[0]


def test_secrets_are_not_printed(monkeypatch, capsys):
    run_workday_autopilot = _load("ops/run_workday_autopilot.py", "v37c_run_workday_autopilot_b")
    monkeypatch.setenv("BETSAPI_KEY", "super-secret-value-12345")
    problems = run_workday_autopilot.validate_env()
    assert problems == []
    captured = capsys.readouterr()
    assert "super-secret-value-12345" not in captured.out
    assert "super-secret-value-12345" not in captured.err


# ------------------------------------------------------- 7: autopilot status missing DB

def test_autopilot_status_handles_missing_settings_row():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()  # no Settings row seeded
    result = autopilot_status.get_status(db)
    assert result["ok"] is False
    assert "error" in result


# ------------------------------------------------------- 8/9: research gates + self-challenge

def test_daily_research_refuses_strong_conclusions_below_thresholds():
    db = _db()
    d = generate_daily_research.section_d_baseline_comparison(db)
    assert d["gate"] == "NOT ENOUGH DATA"
    assert d["distinct_samples"] == 0


def test_market_availability_hypothesis_does_not_overclaim_at_zero_prevalence():
    """Regression test for a real bug found during the v0.3.7C trial run:
    with real prevalence=0.0% (183 combos checked, 0 withdrawn), the
    generator was asserting 'is a real, measurable pattern' -- which is an
    overclaim at 0%. Must phrase a zero-prevalence result as an absence
    finding, not a presence finding."""
    a = {"odds_rows_collected_today": 933, "cumulative_clean_close_count": 21,
        "market_availability_episodes": {
            "withdrawn_prevalence_pct": 0.0,
            "total_match_book_market_selection_combos_checked": 183,
        }}
    b = {"total_classified": 100, "by_primary_state": {"FILLED": 100}}
    d = {"gate": "NOT ENOUGH DATA", "margin_vs_favorite_pts": None, "distinct_samples": 0}
    f = {"clean_count": 0, "retro_count": 0, "total": 0}
    hyps = generate_daily_research.generate_hypotheses(a, b, {}, d, {}, f)
    market_hyp = next((h for h in hyps if h["category"] == "market-availability"), None)
    assert market_hyp is not None
    assert "no" in market_hyp["claim"].lower() or "not" in market_hyp["claim"].lower()
    assert "is a real, measurable pattern" not in market_hyp["claim"]


def test_daily_research_includes_self_challenge():
    db = _db()
    a = generate_daily_research.section_a_data_quality(db)
    b = generate_daily_research.section_b_execution_learning(db)
    c = generate_daily_research.section_c_clv_learning(db)
    d = generate_daily_research.section_d_baseline_comparison(db)
    challenge = generate_daily_research.self_challenge(a, b, c, d)
    for key in ("what_could_make_conclusion_wrong", "hidden_assumption_most_risk", "most_likely_noise",
               "would_waste_most_time_if_chased", "ignore_until_sample_improves",
               "what_forces_tomorrows_plan_to_change"):
        assert key in challenge and challenge[key]


# ------------------------------------------------------- 10: backlog append-only

def test_experiment_backlog_appends_not_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_daily_research, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(generate_daily_research, "BACKLOG_MD", tmp_path / "experiment_backlog.md")
    h1 = [{"claim": "first", "category": "model", "sample_size": 10,
          "why_it_may_be_wrong": "x", "what_kills_it": "y"}]
    h2 = [{"claim": "second", "category": "execution", "sample_size": 20,
          "why_it_may_be_wrong": "x", "what_kills_it": "y"}]
    generate_daily_research.append_backlog(h1, "2026-01-01")
    generate_daily_research.append_backlog(h2, "2026-01-02")
    content = (tmp_path / "experiment_backlog.md").read_text()
    assert "first" in content
    assert "second" in content
    assert "2026-01-01" in content
    assert "2026-01-02" in content


# ------------------------------------------------------- 13: DEGRADED labeling

def test_historical_replay_labeled_degraded():
    db = _db()
    r = run_daily_paper_sim.historical_replay(db)
    assert "DEGRADED" in r["label"]


def test_forward_clean_pending_when_zero_forward_rows():
    db = _db()
    r = run_daily_paper_sim.forward_clean(db)
    assert r["n"] == 0
    assert "PENDING" in r["label"] or "CLEAN" in r["label"]


# ------------------------------------------------------- 14: never blended

def test_filled_only_and_all_signal_clv_never_blended():
    db = _db()
    c = run_daily_paper_sim.clv_first(db)
    assert "historical_provider_time" in c and "forward_system_time" in c
    assert c["historical_provider_time"] is not c["forward_system_time"]
    assert c["historical_provider_time"]["status"] == "DEGRADED"


# ------------------------------------------------------- 15: MarketBaseline included

def test_market_baseline_comparison_included():
    db = _db()
    d = generate_daily_research.section_d_baseline_comparison(db)
    assert "favorite_baseline_pct" in d
    assert "no_edge_baseline_pct" in d
    assert d["no_edge_baseline_pct"] == 50.0


# ------------------------------------------------------- 16/17: friend picks

def test_retro_friend_picks_excluded_and_correlated_legs_grouped(tmp_path, monkeypatch):
    csv_path = tmp_path / "friend_picks.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal_group_id", "leg_id", "source", "clean_scored", "logged_after_result",
                   "price_at_receipt", "book", "market_type"])
        w.writerow(["g1", "1", "Luis", "TRUE", "FALSE", "2.10", "bet365", "ML_3WAY"])
        w.writerow(["g1", "2", "Luis", "TRUE", "FALSE", "1.90", "bet365", "ML_3WAY"])
        w.writerow(["g2", "1", "Luis", "FALSE", "TRUE", "", "", ""])
    monkeypatch.setattr(run_daily_paper_sim, "FRIEND_CSV", csv_path)
    result = run_daily_paper_sim.friend_shadow(None)
    assert result["clean_n"] == 2
    assert result["retro_excluded_n"] == 1
    assert result["correlated_leg_groups_n"] == 1


# ------------------------------------------------------- 18: sample size gates

def test_sample_size_gates_enforced():
    assert run_daily_paper_sim._gate_label(10) == "NOT ENOUGH DATA"
    assert run_daily_paper_sim._gate_label(50) == "DIRECTIONAL"
    assert run_daily_paper_sim._gate_label(150) == "EVIDENCE"
    assert run_daily_paper_sim._gate_label(400) == "DECISION-GRADE"


# ------------------------------------------------------- monitor uses HTTP, not in-process health

def test_monitor_loop_checks_health_via_http_not_in_process():
    """Regression test for a real bug found during the v0.3.7C trial run:
    STATUS (services/poller.py) is per-process. Calling routers.ops.health()
    in-process inside the monitor script always reads the MONITOR's own
    (never-started) STATUS, not the actual backend's -- reporting FAIL even
    when the real backend is healthy and actively collecting. The monitor
    loop must hit the real backend's HTTP endpoint instead."""
    text = (SCRIPTS_DIR / "ops" / "run_workday_autopilot.py").read_text()
    assert "httpx.get(\"http://127.0.0.1:8000/api/ops/health\"" in text
    monitor_loop_start = text.index("while not _shutdown_requested")
    monitor_loop_region = text[monitor_loop_start:monitor_loop_start + 800]
    assert "health_fn(db=db)" not in monitor_loop_region


# ------------------------------------------------------- 19: no live betting modules

def test_no_live_betting_modules_referenced():
    forbidden = ("place_bet", "live_bet", "real_money", "execute_wager", "submit_bet")
    for path in (SCRIPTS_DIR / "ops" / "run_workday_autopilot.py",
                SCRIPTS_DIR / "ops" / "generate_workday_report.py",
                SCRIPTS_DIR / "research" / "generate_daily_research.py",
                SCRIPTS_DIR / "simulations" / "run_daily_paper_sim.py"):
        text = path.read_text().lower()
        for term in forbidden:
            assert term not in text


# ------------------------------------------------------- 20: gitignore coverage

def test_gitignore_covers_backups_logs_env_db():
    gitignore = (REPO_DIR / ".gitignore").read_text()
    for pattern in ("*.db", "backups/", "logs/", ".env", "*.bak", "*.plist"):
        assert pattern in gitignore
