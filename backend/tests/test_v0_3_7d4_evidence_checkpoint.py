"""v0.3.7D.4: evidence checkpoints, sample-growth bottleneck classification,
and the pre-registered kill-criterion/thesis-status decision layer.

Decision-logic tests use hand-built checkpoint dicts (fast, precise,
deterministic coverage of all 7 bottleneck classes). One integration test
seeds real rows through the actual engines to confirm build_checkpoint's
aggregation reconciles against ground truth end-to-end."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.engines import evidence_checkpoint as ec
from app.models import Base, Match, OddsSnapshot, PaperTrade, Player, PredictionLedger, Settings


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    db.add(Settings(id=1))
    db.commit()
    return db


def _gate(distinct=100, strict=20, via_delay=10, research_only=60, late=5, unknown=5,
         clv_n=10, avg_clv=-2.0, awaiting_close=0, pipeline_failures=None,
         valid_close=10, reconciled=True):
    return {
        "lead_time_gate_s": 45.0, "distinct_signals": distinct, "reconciled": reconciled,
        "executable_prekick_strict": strict, "executable_via_start_delay": via_delay,
        "research_only_kickoff": research_only, "late_signal": late, "unknown_start_time": unknown,
        "strict_executable_forward_clv_n": clv_n, "avg_decimal_clv_pct": avg_clv,
        "avg_implied_prob_clv_pct": avg_clv, "valid_closing_price_count": valid_close,
        "signals_awaiting_closing_price": awaiting_close,
        "degraded_close_count_excluded": 0, "excluded_duplicate_count": 0,
        "pipeline_failure_candidates": pipeline_failures or [], "expected_close_window_minutes": 60.0,
    }


def _checkpoint(forward_clean_n=1000, cross_tab_status="OK", cross_tab_reconciled=True,
               collection_has_run=True, gate45=None, gate20=None, gate30=None):
    return {
        "checkpoint_at": "2026-07-14T00:00:00", "run_id": "r", "forward_clean_n": forward_clean_n,
        "cross_tab_status": cross_tab_status, "cross_tab_reconciled": cross_tab_reconciled,
        "collection_has_run": collection_has_run, "collection_evidence_source": "COMPLETED_RUN_METADATA",
        "lead_gates": {"20s": gate20 or _gate(), "30s": gate30 or _gate(), "45s": gate45 or _gate()},
    }


# --------------------------------------------------- bottleneck classifications

def test_report_reconciliation_failure_on_bad_cross_tab():
    cp = _checkpoint(cross_tab_status="FORWARD_REPORTING_UNTRUSTWORTHY", cross_tab_reconciled=False)
    r = ec.classify_bottleneck(cp)
    assert r["classification"] == "REPORT_RECONCILIATION_FAILURE"


def test_report_reconciliation_failure_on_gate_level_mismatch():
    cp = _checkpoint(gate45=_gate(reconciled=False))
    r = ec.classify_bottleneck(cp)
    assert r["classification"] == "REPORT_RECONCILIATION_FAILURE"


def test_collection_did_not_run():
    cp = _checkpoint(collection_has_run=False)
    r = ec.classify_bottleneck(cp)
    assert r["classification"] == "COLLECTION_DID_NOT_RUN"


def test_no_eligible_matches():
    cp = _checkpoint(gate45=_gate(distinct=0, strict=0, via_delay=0, research_only=0, late=0,
                                  unknown=0, clv_n=0, avg_clv=None))
    r = ec.classify_bottleneck(cp)
    assert r["classification"] == "NO_ELIGIBLE_MATCHES"


def test_strict_sample_progressing_when_clv_n_grew():
    prev = _checkpoint(gate45=_gate(clv_n=28))
    cur = _checkpoint(gate45=_gate(clv_n=30, strict=22))
    r = ec.classify_bottleneck(cur, prev)
    assert r["classification"] == "STRICT_SAMPLE_PROGRESSING"
    assert r["detail"]["strict_clv_n_delta"] == 2


def test_closing_pipeline_failure_when_signals_aged_past_window():
    failure = [{"match_id": 1, "selection": "home", "age_minutes": 200.0}]
    prev = _checkpoint(gate45=_gate(strict=20, clv_n=10))
    cur = _checkpoint(gate45=_gate(strict=22, clv_n=10, awaiting_close=1, pipeline_failures=failure))
    r = ec.classify_bottleneck(cur, prev)
    assert r["classification"] == "CLOSING_PIPELINE_FAILURE"
    assert r["detail"]["pipeline_failure_candidates"] == failure


def test_closing_price_pending_when_new_strict_signals_have_no_close_yet_but_within_window():
    prev = _checkpoint(gate45=_gate(strict=20, clv_n=10))
    cur = _checkpoint(gate45=_gate(strict=25, clv_n=10, awaiting_close=5, pipeline_failures=[]))
    r = ec.classify_bottleneck(cur, prev)
    assert r["classification"] == "CLOSING_PRICE_PENDING"
    assert r["detail"]["strict_executable_signals_added"] == 5


def test_strict_executability_scarcity_when_forward_data_grows_but_strict_does_not():
    prev = _checkpoint(forward_clean_n=1000, gate45=_gate(strict=20, clv_n=10, research_only=60))
    cur = _checkpoint(forward_clean_n=1600, gate45=_gate(strict=20, clv_n=10, research_only=460))
    r = ec.classify_bottleneck(cur, prev)
    assert r["classification"] == "STRICT_EXECUTABILITY_SCARCITY"
    assert r["detail"]["forward_clean_signals_added"] == 600
    assert r["detail"]["strict_executable_signals_added"] == 0


def test_first_checkpoint_no_baseline_uses_absolute_state():
    cur = _checkpoint(gate45=_gate(strict=20, clv_n=10, awaiting_close=0))
    r = ec.classify_bottleneck(cur, previous=None)
    assert r["classification"] == "STRICT_SAMPLE_PROGRESSING"

    cur_scarce = _checkpoint(gate45=_gate(strict=0, clv_n=0, avg_clv=None))
    r2 = ec.classify_bottleneck(cur_scarce, previous=None)
    assert r2["classification"] == "STRICT_EXECUTABILITY_SCARCITY"


def test_does_not_infer_from_forward_clean_n_alone():
    """A huge forward_clean_n delta with a genuinely healthy, growing strict
    sample must be STRICT_SAMPLE_PROGRESSING, not STRICT_EXECUTABILITY_SCARCITY
    -- the classifier must check the strict/CLV deltas explicitly, never
    just the total volume of new data."""
    prev = _checkpoint(forward_clean_n=1000, gate45=_gate(strict=20, clv_n=10))
    cur = _checkpoint(forward_clean_n=5000, gate45=_gate(strict=40, clv_n=12))
    r = ec.classify_bottleneck(cur, prev)
    assert r["classification"] == "STRICT_SAMPLE_PROGRESSING"


# --------------------------------------------------- compare_checkpoints / stalled

def test_compare_checkpoints_deltas():
    prev = _checkpoint(forward_clean_n=1000, gate45=_gate(strict=20, clv_n=10, via_delay=5, research_only=60))
    cur = _checkpoint(forward_clean_n=1200, gate45=_gate(strict=25, clv_n=13, via_delay=8, research_only=70))
    d = ec.compare_checkpoints(prev, cur)
    assert d["new_forward_clean_signals"] == 200
    g = d["by_gate"]["45s"]
    assert g["new_strict_executable_prekick"] == 5
    assert g["new_strict_clv_samples"] == 3
    assert g["new_executable_via_start_delay"] == 3
    assert g["new_research_only_kickoff"] == 10


def test_stalled_requires_three_checkpoints():
    cp = _checkpoint(gate45=_gate(clv_n=10))
    assert ec.check_stalled([cp, cp]) is None


def test_stalled_detected_when_n_unchanged_across_two_intervals():
    cp1 = _checkpoint(gate45=_gate(clv_n=10, strict=20))
    cp2 = _checkpoint(gate45=_gate(clv_n=10, strict=20))
    cp3 = _checkpoint(gate45=_gate(clv_n=10, strict=20))
    r = ec.check_stalled([cp1, cp2, cp3])
    assert r is not None
    assert r["stalled"] is True
    assert r["n_history"] == [10, 10, 10]


def test_not_stalled_when_n_grew_across_the_window():
    cp1 = _checkpoint(gate45=_gate(clv_n=10))
    cp2 = _checkpoint(gate45=_gate(clv_n=10))
    cp3 = _checkpoint(gate45=_gate(clv_n=15))
    assert ec.check_stalled([cp1, cp2, cp3]) is None


# --------------------------------------------------- thesis status / kill criterion

def test_insufficient_evidence_below_directional_gate():
    db = _db()
    t = ec.evaluate_thesis_status(db, cross_tab_reconciled=True)
    assert t["thesis_status"] == "INSUFFICIENT_EVIDENCE"
    assert t["kill_criterion_fires"] is False


def test_kill_criterion_never_fires_without_cross_tab_reconciliation(monkeypatch):
    from app.engines import strict_forward_metrics

    def fake_pairs(db, lead_s):
        rows = []
        for i in range(200):
            # entry=1.5, close=2.0 -> clv_pct = 1.5/2.0 - 1 = -25% (materially
            # negative in the real sign convention: entry was WORSE than close).
            s = {"match_id": i, "selection": "home", "current_decimal": 1.5}
            class C:
                close_price_decimal = 2.0
            rows.append((s, C(), None))
        return {"waterfall": {}, "rows": rows}

    def fake_clv(db, lead_s, **kwargs):
        return {"strict_executable_forward_clv_n": 200, "avg_decimal_clv_pct": -2.0}

    monkeypatch.setattr(strict_forward_metrics, "_strict_forward_pairs", fake_pairs)
    monkeypatch.setattr(strict_forward_metrics, "strict_forward_clv", fake_clv)
    db = _db()

    t_not_reconciled = ec.evaluate_thesis_status(db, cross_tab_reconciled=False)
    assert t_not_reconciled["kill_criterion_fires"] is False

    t_reconciled = ec.evaluate_thesis_status(db, cross_tab_reconciled=True)
    # n=200 >= 150, avg=-2.0 <= -1.0 -- CI upper bound must ALSO be < 0 to fire;
    # with 200 identical-CLV (-25%) synthetic rows the CI is degenerate at -25%.
    assert t_reconciled["kill_criterion_fires"] is True
    assert t_reconciled["thesis_status"] == "THESIS_KILL_REVIEW_REQUIRED"


def test_directional_recovery_candidate():
    from app.engines import strict_forward_metrics

    def fake_clv(db, lead_s, **kwargs):
        return {"strict_executable_forward_clv_n": 60, "avg_decimal_clv_pct": 0.5}

    def fake_pairs(db, lead_s):
        return {"waterfall": {}, "rows": []}

    import pytest as _pytest
    m = _pytest.MonkeyPatch()
    m.setattr(strict_forward_metrics, "strict_forward_clv", fake_clv)
    m.setattr(strict_forward_metrics, "_strict_forward_pairs", fake_pairs)
    try:
        db = _db()
        t = ec.evaluate_thesis_status(db, cross_tab_reconciled=True)
        assert t["thesis_status"] == "DIRECTIONAL_RECOVERY_CANDIDATE"
    finally:
        m.undo()


def test_negative_directional_signal():
    from app.engines import strict_forward_metrics

    def fake_clv(db, lead_s, **kwargs):
        return {"strict_executable_forward_clv_n": 60, "avg_decimal_clv_pct": -0.3}

    def fake_pairs(db, lead_s):
        return {"waterfall": {}, "rows": []}

    import pytest as _pytest
    m = _pytest.MonkeyPatch()
    m.setattr(strict_forward_metrics, "strict_forward_clv", fake_clv)
    m.setattr(strict_forward_metrics, "_strict_forward_pairs", fake_pairs)
    try:
        db = _db()
        t = ec.evaluate_thesis_status(db, cross_tab_reconciled=True)
        assert t["thesis_status"] == "NEGATIVE_DIRECTIONAL_SIGNAL"
        assert t["kill_criterion_fires"] is False
    finally:
        m.undo()


# --------------------------------------------------- build_checkpoint integration

def _seed_one_forward_trustworthy_trade(db, now: datetime):
    h = Player(name="H", league="L")
    a = Player(name="A", league="L")
    db.add_all([h, a])
    db.flush()
    start = now - timedelta(hours=1)
    m = Match(start_time=start, league="L", home_player_id=h.id, away_player_id=a.id,
             source="betsapi", verification_status="api_verified")
    db.add(m)
    db.flush()
    pred = PredictionLedger(
        match_id=m.id, horizon_label="T-5m", prediction_time=start - timedelta(minutes=5),
        scheduled_start=start, model_version="v", sportsbook="bet365", market="ML_3WAY",
        selection="home", current_decimal=2.2, predicted_winner="home", model_prob=0.5,
        maximum_entry_decimal=2.2, action="WAIT", status="scored",
        immutable_hash=f"h-{m.id}-home-{(start - timedelta(minutes=5)).isoformat()}")
    db.add(pred)
    db.flush()
    snap = OddsSnapshot(match_id=m.id, sportsbook="bet365", market="ML_3WAY", selection="home",
                       american_odds=100, decimal_odds=2.2, implied_prob=round(1 / 2.2, 4),
                       collected_at=start - timedelta(minutes=5), phase="pre", data_source="betsapi",
                       verification_status="api_verified",
                       polled_at=start - timedelta(minutes=5), ingested_at=start - timedelta(minutes=5),
                       response_received_at=start - timedelta(minutes=5))
    db.add(snap)
    db.flush()
    trade = PaperTrade(match_id=m.id, signal_id=pred.id, signal_source="MODEL", delay_seconds=30,
                       selection="home", settlement_status="FILLED",
                       created_at=start - timedelta(minutes=5), signal_time=start - timedelta(minutes=5),
                       market="ML_3WAY", sportsbook="bet365", price_snapshot_id=snap.id,
                       price_decimal=2.2)
    db.add(trade)
    db.commit()
    return m, pred


def test_build_checkpoint_reconciles_on_seeded_data():
    db = _db()
    now = datetime(2026, 7, 14, 2, 0, 0)
    _seed_one_forward_trustworthy_trade(db, now)
    cp = ec.build_checkpoint(db, now, run_id="seed-test")
    for gate_key in ("20s", "30s", "45s"):
        g = cp["lead_gates"][gate_key]
        assert g["distinct_signals"] == 1
        total = (g["executable_prekick_strict"] + g["executable_via_start_delay"]
                + g["research_only_kickoff"] + g["late_signal"] + g["unknown_start_time"])
        assert total == g["distinct_signals"]
        assert g["reconciled"] is True
    assert cp["cross_tab_reconciled"] is True
