"""v0.3.7D.1 Task 7: deterministic verdict hierarchy.

Exactly one of the ten branches below fires, in this fixed order --
first-match-wins. Every call reports which branch fired AND why every
earlier branch did NOT fire, so the reasoning is always auditable (Task 7
requirement). Historical/degraded data never participates in this decision
-- it is annotation only (see clv_forward_readiness.historical_clv_report,
always roi_descriptive_only=True).

Thresholds reuse the same directional/decision-grade sample gates already
used elsewhere in this release (strict_forward_metrics, clv_forward_readiness)
rather than inventing new ones.
"""
from __future__ import annotations

MATERIALLY_NEGATIVE_CLV_PCT = -0.5
EXECUTION_BLOCKED_RATE = 0.7
DIRECTIONAL_MIN_N = 50
DECISION_MIN_N = 150
BASELINE_SIGNIFICANCE_MIN_N = 50

BRANCHES = (
    "COLLECTION_NOT_RUN",
    "FORWARD_REPORTING_UNTRUSTWORTHY",
    "FORWARD_SAMPLE_INSUFFICIENT",
    "FORWARD_CLV_INSUFFICIENT",
    "EXECUTION_BLOCKED",
    "MODEL_NEGATIVE_CLV",
    "MODEL_UNDERPERFORMS_BASELINE",
    "NO_DEMONSTRATED_EDGE",
    "DIRECTIONAL_EDGE_CANDIDATE",
    "CLEAN_FORWARD_EDGE_CANDIDATE",
)


def _fire(branch: str, reasons: list[str], why: str) -> dict:
    return {
        "verdict": branch,
        "why_this_branch_fired": why,
        "branch_order": list(BRANCHES),
        "why_earlier_branches_did_not_fire": list(reasons),
    }


def determine_verdict(*, collection_has_run: bool, active_collection_window: bool,
                      cross_tab: dict, strict_clv: dict, paired: dict) -> dict:
    """`cross_tab` = strict_forward_metrics.forward_executability_primary_state_cross_tab().
    `strict_clv` = strict_forward_metrics.strict_forward_clv() at the release's
    primary lead-time gate (20s). `paired` =
    strict_forward_metrics.paired_market_baseline_comparison() at the same gate."""
    reasons: list[str] = []

    # 1. COLLECTION_NOT_RUN
    if active_collection_window and not collection_has_run:
        return _fire("COLLECTION_NOT_RUN", reasons,
                    "Active collection window with no run recorded yet.")
    reasons.append("COLLECTION_NOT_RUN: did not fire -- a run has occurred, or no active window is expected now.")

    # 2. FORWARD_REPORTING_UNTRUSTWORTHY
    if cross_tab.get("status") != "OK":
        return _fire("FORWARD_REPORTING_UNTRUSTWORTHY", reasons,
                    f"Cross-tab reconciliation failed: status={cross_tab.get('status')}.")
    reasons.append("FORWARD_REPORTING_UNTRUSTWORTHY: did not fire -- cross-tab reconciled exactly "
                   "(row/col totals match forward_clean_n, zero unrecognized rows).")

    strict_executable_n = cross_tab.get("row_totals", {}).get("EXECUTABLE_PREKICK_STRICT", 0)
    # 3. FORWARD_SAMPLE_INSUFFICIENT
    if strict_executable_n < DIRECTIONAL_MIN_N:
        return _fire("FORWARD_SAMPLE_INSUFFICIENT", reasons,
                    f"Strict executable-forward n={strict_executable_n} < {DIRECTIONAL_MIN_N}.")
    reasons.append(f"FORWARD_SAMPLE_INSUFFICIENT: did not fire -- strict executable-forward "
                   f"n={strict_executable_n} >= {DIRECTIONAL_MIN_N}.")

    clv_n = strict_clv.get("strict_executable_forward_clv_n", 0)
    # 4. FORWARD_CLV_INSUFFICIENT
    if clv_n < DIRECTIONAL_MIN_N:
        return _fire("FORWARD_CLV_INSUFFICIENT", reasons,
                    f"Strict CLV-computable n={clv_n} < {DIRECTIONAL_MIN_N} "
                    "(requires valid close quality + complete 3-way market, on top of strict executability).")
    reasons.append(f"FORWARD_CLV_INSUFFICIENT: did not fire -- strict CLV n={clv_n} >= {DIRECTIONAL_MIN_N}.")

    # 5. EXECUTION_BLOCKED
    primary_dist = cross_tab.get("cross_tab", {}).get("EXECUTABLE_PREKICK_STRICT", {})
    blocked = (primary_dist.get("NO_DATA_AT_ENTRY", 0) + primary_dist.get("MARKET_UNAVAILABLE_AT_ENTRY", 0)
              + primary_dist.get("BOOK_MISSING_MARKET", 0))
    blocked_rate = blocked / strict_executable_n if strict_executable_n else 0.0
    if blocked_rate > EXECUTION_BLOCKED_RATE:
        return _fire("EXECUTION_BLOCKED", reasons,
                    f"{blocked_rate:.0%} of strict executable rows are NO_DATA/unavailable "
                    f"(> {EXECUTION_BLOCKED_RATE:.0%}).")
    reasons.append(f"EXECUTION_BLOCKED: did not fire -- unavailability rate {blocked_rate:.0%} "
                   f"<= {EXECUTION_BLOCKED_RATE:.0%}.")

    avg_clv = strict_clv.get("avg_decimal_clv_pct")
    # 6. MODEL_NEGATIVE_CLV
    if avg_clv is not None and avg_clv < MATERIALLY_NEGATIVE_CLV_PCT:
        return _fire("MODEL_NEGATIVE_CLV", reasons,
                    f"avg strict decimal CLV={avg_clv}% is materially negative "
                    f"(< {MATERIALLY_NEGATIVE_CLV_PCT}%).")
    reasons.append(f"MODEL_NEGATIVE_CLV: did not fire -- avg strict decimal CLV={avg_clv}% "
                   "not materially negative.")

    # 7. MODEL_UNDERPERFORMS_BASELINE
    paired_n = paired.get("scored_n", 0)
    if paired.get("significant_baseline_outperformance") and paired_n >= BASELINE_SIGNIFICANCE_MIN_N:
        return _fire("MODEL_UNDERPERFORMS_BASELINE", reasons,
                    f"Paired McNemar test shows significant baseline outperformance on n={paired_n} "
                    "strict, deduped, unique-event rows.")
    reasons.append("MODEL_UNDERPERFORMS_BASELINE: did not fire -- paired comparison not significant, "
                   f"or n={paired_n} below the {BASELINE_SIGNIFICANCE_MIN_N}-sample significance gate.")

    # 8/9/10: neutral vs. positive, gated by sample size
    if avg_clv is None or avg_clv <= 0:
        return _fire("NO_DEMONSTRATED_EDGE", reasons,
                    f"avg strict decimal CLV={avg_clv}% is neutral/non-positive.")
    reasons.append("NO_DEMONSTRATED_EDGE: did not fire -- avg strict decimal CLV is positive.")

    if clv_n < DECISION_MIN_N:
        return _fire("DIRECTIONAL_EDGE_CANDIDATE", reasons,
                    f"Positive avg CLV={avg_clv}% but n={clv_n} < decision-grade threshold {DECISION_MIN_N}.")
    reasons.append(f"DIRECTIONAL_EDGE_CANDIDATE: did not fire -- n={clv_n} >= {DECISION_MIN_N}.")

    return _fire("CLEAN_FORWARD_EDGE_CANDIDATE", reasons,
                f"Positive avg CLV={avg_clv}% on n={clv_n} >= {DECISION_MIN_N}, reconciled cross-tab, "
                "paired comparison does not show significant baseline outperformance.")
