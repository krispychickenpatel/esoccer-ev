"""v0.3.7D.3: recommendation engine unified on shared collection evidence.

v0.3.7D.1's build_recommendation() derived its run-state signal directly
from health['active_run'] / health['last_completed_run'] -- the same raw
fields that made verdict_hierarchy's COLLECTION_NOT_RUN branch produce
false negatives on migration-boundary data (fixed in D.2 by
collection_evidence.resolve_collection_evidence()). Because the
recommendation never adopted that fix, the same report could show
verdict=FORWARD_CLV_INSUFFICIENT (a real run, evidence-based) next to
recommendation=NO RUN RECORDED (raw last_completed_run is NULL) --
an internal contradiction. This module now resolves evidence exactly once
(collection_evidence.resolve_collection_evidence) and, once collection is
known to have run, derives its action from verdict_hierarchy.determine_verdict()
itself rather than re-deriving thresholds independently -- so the two can
never disagree on which decisional branch fired.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import collection_evidence, strict_forward_metrics, verdict_hierarchy

DEFAULT_LEAD_GATE_S = 20.0
DIRECTIONAL_MIN_N = verdict_hierarchy.DIRECTIONAL_MIN_N
DECISION_MIN_N = verdict_hierarchy.DECISION_MIN_N

_EVIDENCE_PHRASE = {
    collection_evidence.ACTIVE_RUN: "collection is currently active",
    collection_evidence.COMPLETED_RUN_METADATA: "a recently completed run is recorded",
    collection_evidence.LEGACY_RECENT_ACTIVITY_INFERRED:
        "recent collection inferred from valid operational evidence",
}

_BELOW_DECISION_GATE_ACTION = "CONTINUE_EVIDENCE_COLLECTION"
_AT_DECISION_GATE_ACTION = "REVIEW_MODEL_KILL_OR_REBUILD"


def _gate_action(n: int) -> str:
    return _BELOW_DECISION_GATE_ACTION if n < DECISION_MIN_N else _AT_DECISION_GATE_ACTION


def _action_and_reason(branch: str, n: int, avg_clv, blocked_rate: float | None) -> tuple[str, str]:
    if branch in ("FORWARD_SAMPLE_INSUFFICIENT", "FORWARD_CLV_INSUFFICIENT"):
        return "CONTINUE_DAILY_COLLECTION", f"strict executable-forward CLV n={n}/{DIRECTIONAL_MIN_N} directional gate"
    if branch == "EXECUTION_BLOCKED":
        rate = f"{blocked_rate:.0%}" if blocked_rate is not None else "majority"
        return "REVIEW_EXECUTION_OR_SOURCE", f"{rate} of strict executable rows are NO_DATA/unavailable at entry"
    if branch == "MODEL_NEGATIVE_CLV":
        return _gate_action(n), f"negative directional CLV, n={n}/{DECISION_MIN_N} decision gate, avg decimal CLV={avg_clv}%"
    if branch == "MODEL_UNDERPERFORMS_BASELINE":
        return _gate_action(n), f"paired comparison shows significant baseline outperformance, n={n}/{DECISION_MIN_N} decision gate"
    if branch == "NO_DEMONSTRATED_EDGE":
        return _gate_action(n), f"avg decimal CLV={avg_clv}% is neutral/non-positive, n={n}/{DECISION_MIN_N} decision gate"
    if branch == "DIRECTIONAL_EDGE_CANDIDATE":
        return "CONTINUE_EDGE_VALIDATION", f"positive avg decimal CLV={avg_clv}% but n={n} below decision-grade threshold {DECISION_MIN_N}"
    if branch == "CLEAN_FORWARD_EDGE_CANDIDATE":
        return "EDGE_CONFIRMED_DECISION_GRADE", f"positive avg decimal CLV={avg_clv}% at decision-grade n={n}"
    raise AssertionError(
        f"unhandled verdict branch in recommendation mapping: {branch!r} "
        "(COLLECTION_NOT_RUN/FORWARD_REPORTING_UNTRUSTWORTHY are handled before this is reached)")


def build_recommendation(db: Session, health: dict, now: datetime | None = None,
                         lead_s: float = DEFAULT_LEAD_GATE_S, evidence: dict | None = None,
                         cross_tab: dict | None = None, strict_clv: dict | None = None,
                         paired: dict | None = None) -> dict:
    """`health` is the dict returned by app.routers.ops.health() (or the real
    backend's /api/ops/health JSON). `evidence`/`cross_tab`/`strict_clv`/`paired`
    may be passed in by a caller that already computed them (e.g. alongside a
    verdict_hierarchy.determine_verdict() call) to avoid recomputing the
    expensive cross-tab pass twice in the same report; each is computed here
    via the same shared engines if omitted."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    if evidence is None:
        evidence = collection_evidence.resolve_collection_evidence(db, health, now)
    if strict_clv is None:
        strict_clv = strict_forward_metrics.strict_forward_clv(db, lead_s)

    n = strict_clv["strict_executable_forward_clv_n"]
    avg_clv = strict_clv.get("avg_decimal_clv_pct")
    detail = evidence.get("detail", {})

    base = {
        "collection_has_run": evidence["collection_has_run"],
        "collection_evidence_source": evidence["evidence_source"],
        "evidence_window_start": detail.get("evidence_window_start"),
        "evidence_window_end": detail.get("evidence_window_end"),
        "strict_executable_clv_n": n,
        "strict_executable_clv_lead_gate_s": lead_s,
        "avg_decimal_clv_pct": avg_clv,
        "active_run": health.get("active_run"),
        "last_completed_run": health.get("last_completed_run"),
        "collection_status": health.get("status"),
    }

    if not evidence["collection_has_run"]:
        reason = "no qualifying collection evidence exists in the reporting window"
        return {
            **base,
            "action": "START_COLLECTION",
            "reason": reason,
            "next_evidence_gate": f"n={n}/{DIRECTIONAL_MIN_N} directional gate",
            "message": f"START_COLLECTION -- {reason}.",
        }

    if cross_tab is None:
        cross_tab = strict_forward_metrics.forward_executability_primary_state_cross_tab(db)
    if paired is None:
        paired = strict_forward_metrics.paired_market_baseline_comparison(db, lead_s=lead_s)

    active_window = bool(health.get("expected_collection_window_active", True))
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=evidence["collection_has_run"], active_collection_window=active_window,
        cross_tab=cross_tab, strict_clv=strict_clv, paired=paired)

    if verdict["verdict"] == "FORWARD_REPORTING_UNTRUSTWORTHY":
        reason = f"forward cross-tab reconciliation failed (status={cross_tab.get('status')})"
        return {
            **base,
            "action": "FIX_FORWARD_REPORTING",
            "reason": reason,
            "next_evidence_gate": f"n={n}/{DIRECTIONAL_MIN_N} directional gate",
            "message": f"FIX_FORWARD_REPORTING -- {reason}.",
        }

    strict_executable_n = cross_tab.get("row_totals", {}).get("EXECUTABLE_PREKICK_STRICT", 0)
    primary_dist = cross_tab.get("cross_tab", {}).get("EXECUTABLE_PREKICK_STRICT", {})
    blocked = (primary_dist.get("NO_DATA_AT_ENTRY", 0) + primary_dist.get("MARKET_UNAVAILABLE_AT_ENTRY", 0)
              + primary_dist.get("BOOK_MISSING_MARKET", 0))
    blocked_rate = blocked / strict_executable_n if strict_executable_n else None

    action, reason = _action_and_reason(verdict["verdict"], n, avg_clv, blocked_rate)
    gate = (f"n={n}/{DIRECTIONAL_MIN_N} directional gate" if n < DIRECTIONAL_MIN_N
           else f"n={n}/{DECISION_MIN_N} decision gate" if n < DECISION_MIN_N
           else f"n={n} decision-grade (>= {DECISION_MIN_N})")

    if action == "CONTINUE_DAILY_COLLECTION":
        evidence_phrase = _EVIDENCE_PHRASE.get(evidence["evidence_source"], "collection evidence present")
        message = (f"CONTINUE_DAILY_COLLECTION -- {evidence_phrase}; "
                  f"strict executable-forward CLV n={n}/{DIRECTIONAL_MIN_N}, avg decimal CLV={avg_clv}%.")
    else:
        message = f"{action} -- {reason}."

    return {
        **base,
        "action": action,
        "reason": reason,
        "next_evidence_gate": gate,
        "message": message,
        "verdict_branch": verdict["verdict"],
    }


def check_evidence_consistency(verdict: dict, recommendation: dict) -> dict:
    """Task 3: the verdict and recommendation must agree on collection
    evidence -- both are derived from the same resolve_collection_evidence()
    call, but this is a cheap, explicit assertion of that invariant rather
    than trusting it silently. Returns flag=RECOMMENDATION_EVIDENCE_MISMATCH
    (never raises) so callers can decide how to fail closed."""
    v_has_run = verdict.get("collection_has_run")
    r_has_run = recommendation.get("collection_has_run")
    v_source = verdict.get("collection_run_evidence_source")
    r_source = recommendation.get("collection_evidence_source")
    consistent = (v_has_run == r_has_run) and (v_source == r_source)
    return {
        "consistent": consistent,
        "flag": None if consistent else "RECOMMENDATION_EVIDENCE_MISMATCH",
        "verdict_collection_has_run": v_has_run,
        "recommendation_collection_has_run": r_has_run,
        "verdict_evidence_source": v_source,
        "recommendation_evidence_source": r_source,
    }
