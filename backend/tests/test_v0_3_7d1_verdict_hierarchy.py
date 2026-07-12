"""v0.3.7D.1 Task 7/12: every branch of the deterministic verdict hierarchy
fires exactly as specified, in the exact fixed order, with no live DB
needed -- pure function over constructed report dicts."""
from app.engines import verdict_hierarchy as vh

OK_CROSS_TAB_BASE = {
    "status": "OK",
    "row_totals": {"EXECUTABLE_PREKICK_STRICT": 100},
    "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"NO_DATA_AT_ENTRY": 5, "FILLED": 95}},
}
OK_STRICT_CLV_BASE = {"strict_executable_forward_clv_n": 100, "avg_decimal_clv_pct": 0.0}
OK_PAIRED_BASE = {"scored_n": 100, "significant_baseline_outperformance": False}


def _call(**overrides):
    kwargs = {
        "collection_has_run": True,
        "active_collection_window": False,
        "cross_tab": OK_CROSS_TAB_BASE,
        "strict_clv": OK_STRICT_CLV_BASE,
        "paired": OK_PAIRED_BASE,
    }
    kwargs.update(overrides)
    return vh.determine_verdict(**kwargs)


def test_branch_1_collection_not_run():
    r = _call(active_collection_window=True, collection_has_run=False)
    assert r["verdict"] == "COLLECTION_NOT_RUN"
    assert r["why_earlier_branches_did_not_fire"] == []


def test_branch_2_forward_reporting_untrustworthy():
    cross_tab = {**OK_CROSS_TAB_BASE, "status": "FORWARD_REPORTING_UNTRUSTWORTHY"}
    r = _call(cross_tab=cross_tab)
    assert r["verdict"] == "FORWARD_REPORTING_UNTRUSTWORTHY"
    assert any("COLLECTION_NOT_RUN" in x for x in r["why_earlier_branches_did_not_fire"])


def test_branch_3_forward_sample_insufficient():
    cross_tab = {**OK_CROSS_TAB_BASE, "row_totals": {"EXECUTABLE_PREKICK_STRICT": 10}}
    r = _call(cross_tab=cross_tab)
    assert r["verdict"] == "FORWARD_SAMPLE_INSUFFICIENT"


def test_branch_4_forward_clv_insufficient():
    strict_clv = {**OK_STRICT_CLV_BASE, "strict_executable_forward_clv_n": 20}
    r = _call(strict_clv=strict_clv)
    assert r["verdict"] == "FORWARD_CLV_INSUFFICIENT"


def test_branch_5_execution_blocked():
    cross_tab = {**OK_CROSS_TAB_BASE,
                "cross_tab": {"EXECUTABLE_PREKICK_STRICT": {"NO_DATA_AT_ENTRY": 80, "FILLED": 20}}}
    r = _call(cross_tab=cross_tab)
    assert r["verdict"] == "EXECUTION_BLOCKED"


def test_branch_6_model_negative_clv():
    strict_clv = {**OK_STRICT_CLV_BASE, "avg_decimal_clv_pct": -2.0}
    r = _call(strict_clv=strict_clv)
    assert r["verdict"] == "MODEL_NEGATIVE_CLV"


def test_branch_7_model_underperforms_baseline():
    strict_clv = {**OK_STRICT_CLV_BASE, "avg_decimal_clv_pct": 0.1}
    paired = {"scored_n": 60, "significant_baseline_outperformance": True}
    r = _call(strict_clv=strict_clv, paired=paired)
    assert r["verdict"] == "MODEL_UNDERPERFORMS_BASELINE"


def test_branch_7_does_not_fire_below_significance_sample_gate():
    """Paired significance with too few scored rows must not trigger the
    verdict -- otherwise a tiny discordant-pair count could swing the
    headline result."""
    strict_clv = {**OK_STRICT_CLV_BASE, "avg_decimal_clv_pct": 0.1}
    paired = {"scored_n": 10, "significant_baseline_outperformance": True}
    r = _call(strict_clv=strict_clv, paired=paired)
    assert r["verdict"] != "MODEL_UNDERPERFORMS_BASELINE"


def test_branch_8_no_demonstrated_edge():
    strict_clv = {**OK_STRICT_CLV_BASE, "avg_decimal_clv_pct": -0.1}
    r = _call(strict_clv=strict_clv)
    assert r["verdict"] == "NO_DEMONSTRATED_EDGE"


def test_branch_9_directional_edge_candidate():
    strict_clv = {"strict_executable_forward_clv_n": 100, "avg_decimal_clv_pct": 0.3}
    r = _call(strict_clv=strict_clv)
    assert r["verdict"] == "DIRECTIONAL_EDGE_CANDIDATE"


def test_branch_10_clean_forward_edge_candidate():
    cross_tab = {**OK_CROSS_TAB_BASE, "row_totals": {"EXECUTABLE_PREKICK_STRICT": 200}}
    strict_clv = {"strict_executable_forward_clv_n": 200, "avg_decimal_clv_pct": 0.3}
    paired = {"scored_n": 200, "significant_baseline_outperformance": False}
    r = _call(cross_tab=cross_tab, strict_clv=strict_clv, paired=paired)
    assert r["verdict"] == "CLEAN_FORWARD_EDGE_CANDIDATE"


def test_branch_order_matches_spec():
    assert vh.BRANCHES == (
        "COLLECTION_NOT_RUN", "FORWARD_REPORTING_UNTRUSTWORTHY", "FORWARD_SAMPLE_INSUFFICIENT",
        "FORWARD_CLV_INSUFFICIENT", "EXECUTION_BLOCKED", "MODEL_NEGATIVE_CLV",
        "MODEL_UNDERPERFORMS_BASELINE", "NO_DEMONSTRATED_EDGE", "DIRECTIONAL_EDGE_CANDIDATE",
        "CLEAN_FORWARD_EDGE_CANDIDATE",
    )
