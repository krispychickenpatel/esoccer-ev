"""v0.3.7D.4: automated evidence checkpoints and sample-growth bottleneck
classification.

Read-only report layer. Every number here is produced by calling an
existing, already-tested engine function -- `strict_forward_metrics`,
`execution_classifier_v2.compute_executability`, `closing_records._actual_start`,
`collection_evidence` -- this module never reimplements executability,
close-quality, or CLV rules. It only aggregates their per-row output into
counts that don't already exist as a report (signals still waiting on a
closing price, vs. signals whose closing-price window has already passed
with nothing produced).

Expected closing-price window: this codebase's own ClosingRecord pipeline
(`closing_records.build_all`) only processes a match once `Match.home_score`
is populated by `ingest_ended_results` (poller.py, throttled to once/45s),
and only runs on the poller's own auto-paper-sim cycle (once/15min, see
`AUTO_PAPER_SIM_INTERVAL_S` in services/poller.py) -- it does not fire the
instant a pre-kickoff close price is technically available. Measured
empirically against this repo's own real data (actual live-start ->
Match.updated_at once the final score lands): median 671s, p90 882s, max
2360s across 903 settled matches -- consistent with this deployment's
tracked eSoccer leagues, which run 6-12 minutes. Default expected window is
therefore set to 60 minutes after actual kickoff (p90 + a full 15-minute
auto-cycle + a safety margin), configurable via
EVIDENCE_EXPECTED_CLOSE_WINDOW_MINUTES. A strict executable signal older
than this with no closing record is a CLOSING_PIPELINE_FAILURE candidate,
not normal pending latency.
"""
from __future__ import annotations

import os
import random
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClosingRecord, Match, PredictionLedger
from . import closing_records as closing_records_engine
from . import execution_classifier_v2 as ecv2
from . import strict_forward_metrics, winner_edge

LEAD_TIME_GATES_S = ecv2.LEAD_TIME_GATES_S  # (20.0, 30.0, 45.0)
PRIMARY_BOTTLENECK_GATE_S = 45.0

DEFAULT_EXPECTED_CLOSE_WINDOW_MINUTES = 60.0

BOTTLENECK_CLASSES = (
    "STRICT_SAMPLE_PROGRESSING",
    "CLOSING_PRICE_PENDING",
    "CLOSING_PIPELINE_FAILURE",
    "STRICT_EXECUTABILITY_SCARCITY",
    "NO_ELIGIBLE_MATCHES",
    "REPORT_RECONCILIATION_FAILURE",
    "COLLECTION_DID_NOT_RUN",
)


def _expected_close_window_minutes() -> float:
    try:
        return float(os.environ.get("EVIDENCE_EXPECTED_CLOSE_WINDOW_MINUTES",
                                    DEFAULT_EXPECTED_CLOSE_WINDOW_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_EXPECTED_CLOSE_WINDOW_MINUTES


def _lead_gate_snapshot(db: Session, lead_s: float, now: datetime,
                        expected_window_minutes: float) -> dict:
    """One lead-time gate's full signal-count breakdown. Iterates the same
    distinct (match, selection) sample set strict_forward_metrics uses
    (`winner_edge._model_samples`), calling the existing, unmodified
    `compute_executability` per row -- this is a new AGGREGATION, not a new
    classification rule."""
    samples = winner_edge._model_samples(db)
    counts = {"executable_prekick_strict": 0, "executable_via_start_delay": 0,
             "research_only_kickoff": 0, "late_signal": 0, "unknown_start_time": 0}
    label_to_key = {
        ecv2.EXECUTABLE_PREKICK_STRICT: "executable_prekick_strict",
        ecv2.EXECUTABLE_VIA_START_DELAY: "executable_via_start_delay",
        ecv2.RESEARCH_ONLY_KICKOFF: "research_only_kickoff",
        ecv2.LATE_SIGNAL: "late_signal",
        ecv2.UNKNOWN_START_TIME: "unknown_start_time",
    }
    signals_awaiting_close = 0
    valid_closing_price_count = 0
    degraded_close_count = 0  # has a close, but not forward-clean (no system timestamps)
    pipeline_failure_candidates = []

    for s in samples:
        pred = db.get(PredictionLedger, s["prediction_id"])
        match = db.get(Match, s["match_id"])
        label = ecv2.compute_executability(db, pred, match, min_lead_s=lead_s)
        counts[label_to_key.get(label, "unknown_start_time")] += 1

        if label != ecv2.EXECUTABLE_PREKICK_STRICT:
            continue

        close = db.scalar(select(ClosingRecord).where(
            ClosingRecord.match_id == s["match_id"], ClosingRecord.sportsbook == "bet365",
            ClosingRecord.market == "ML_3WAY", ClosingRecord.selection == s["selection"]))
        if close is None or close.close_price_decimal is None:
            signals_awaiting_close += 1
            actual_start, _used_fallback = closing_records_engine._actual_start(db, match)
            age_minutes = ((now - actual_start).total_seconds() / 60.0) if actual_start else None
            if age_minutes is not None and age_minutes > expected_window_minutes:
                pipeline_failure_candidates.append({
                    "match_id": s["match_id"], "selection": s["selection"],
                    "actual_start": actual_start.isoformat() if actual_start else None,
                    "age_minutes": round(age_minutes, 1),
                })
        else:
            valid_closing_price_count += 1
            if close.close_polled_at is None or close.close_ingested_at is None:
                degraded_close_count += 1

    strict_clv = strict_forward_metrics.strict_forward_clv(db, lead_s)
    distinct_signals = len(samples)
    accounted = sum(counts.values())

    return {
        "lead_time_gate_s": lead_s,
        "distinct_signals": distinct_signals,
        "reconciled": accounted == distinct_signals,
        **counts,
        "strict_executable_forward_clv_n": strict_clv["strict_executable_forward_clv_n"],
        "avg_decimal_clv_pct": strict_clv["avg_decimal_clv_pct"],
        "avg_implied_prob_clv_pct": strict_clv["avg_implied_prob_clv_pct"],
        "valid_closing_price_count": valid_closing_price_count,
        "signals_awaiting_closing_price": signals_awaiting_close,
        "degraded_close_count_excluded": degraded_close_count,
        "excluded_duplicate_count": strict_clv["exclusion_waterfall"].get("duplicate_signals_removed", 0),
        "pipeline_failure_candidates": pipeline_failure_candidates,
        "expected_close_window_minutes": expected_window_minutes,
    }


def build_checkpoint(db: Session, now: datetime, run_id: str | None = None,
                     collection_evidence_result: dict | None = None) -> dict:
    """Full checkpoint: per-gate snapshot at 20s/30s/45s, plus the shared
    cross-tab reconciliation status (reused from strict_forward_metrics --
    computed once, since it is lead-gate-independent: stored
    ExecutionClassification rows are always classified at the 20s gate)."""
    expected_window = _expected_close_window_minutes()
    cross_tab = strict_forward_metrics.forward_executability_primary_state_cross_tab(db)
    gates = {}
    for lead_s in LEAD_TIME_GATES_S:
        gates[f"{int(lead_s)}s"] = _lead_gate_snapshot(db, lead_s, now, expected_window)

    return {
        "checkpoint_at": now.isoformat(),
        "run_id": run_id,
        "forward_clean_n": cross_tab["forward_clean_n"],
        "cross_tab_status": cross_tab["status"],
        "cross_tab_reconciled": cross_tab["reconciled"],
        "collection_has_run": (collection_evidence_result or {}).get("collection_has_run"),
        "collection_evidence_source": (collection_evidence_result or {}).get("evidence_source"),
        "lead_gates": gates,
    }


def compare_checkpoints(previous: dict, current: dict) -> dict:
    """Deltas since the previous checkpoint, per gate plus overall
    forward-clean growth. Uses checkpoint timestamps/run_ids, never calendar
    day boundaries."""
    deltas = {
        "previous_checkpoint_at": previous.get("checkpoint_at"),
        "current_checkpoint_at": current.get("checkpoint_at"),
        "new_forward_clean_signals": current["forward_clean_n"] - previous["forward_clean_n"],
        "by_gate": {},
    }
    for gate_key, cur_gate in current["lead_gates"].items():
        prev_gate = previous["lead_gates"].get(gate_key, {})
        deltas["by_gate"][gate_key] = {
            "new_strict_executable_prekick": (cur_gate["executable_prekick_strict"]
                                              - prev_gate.get("executable_prekick_strict", 0)),
            "new_executable_via_start_delay": (cur_gate["executable_via_start_delay"]
                                               - prev_gate.get("executable_via_start_delay", 0)),
            "new_research_only_kickoff": (cur_gate["research_only_kickoff"]
                                          - prev_gate.get("research_only_kickoff", 0)),
            "new_valid_closing_records": (cur_gate["valid_closing_price_count"]
                                         - prev_gate.get("valid_closing_price_count", 0)),
            "new_strict_clv_samples": (cur_gate["strict_executable_forward_clv_n"]
                                      - prev_gate.get("strict_executable_forward_clv_n", 0)),
        }
    return deltas


def classify_bottleneck(current: dict, previous: dict | None = None) -> dict:
    """One of BOTTLENECK_CLASSES, with the counts that justify it. Never
    infers from forward_clean_n alone -- always compares the primary gate's
    (45s) strict-executable and CLV-sample counts explicitly."""
    if not current.get("cross_tab_reconciled", True):
        return {"classification": "REPORT_RECONCILIATION_FAILURE",
               "detail": f"cross_tab_status={current.get('cross_tab_status')}"}

    gate = current["lead_gates"][f"{int(PRIMARY_BOTTLENECK_GATE_S)}s"]
    if not gate.get("reconciled", True):
        return {"classification": "REPORT_RECONCILIATION_FAILURE",
               "detail": f"{PRIMARY_BOTTLENECK_GATE_S}s gate signal-count reconciliation failed"}

    if current.get("collection_has_run") is False:
        return {"classification": "COLLECTION_DID_NOT_RUN",
               "detail": f"evidence_source={current.get('collection_evidence_source')}"}

    if gate["distinct_signals"] == 0:
        return {"classification": "NO_ELIGIBLE_MATCHES",
               "detail": "zero distinct match/selection signals exist to classify"}

    counts = {
        "forward_clean_signals_added": None,
        "strict_executable_signals_added": None,
        "via_start_delay_added": gate["executable_via_start_delay"],
        "research_only_kickoff_added": gate["research_only_kickoff"],
        "strict_clv_n_delta": None,
        "pipeline_failure_candidates": gate["pipeline_failure_candidates"],
        "signals_awaiting_closing_price": gate["signals_awaiting_closing_price"],
    }

    if previous is None:
        # No baseline to diff against -- classify from absolute state only.
        if gate["pipeline_failure_candidates"]:
            return {"classification": "CLOSING_PIPELINE_FAILURE", "detail": counts}
        if gate["signals_awaiting_closing_price"] > 0 and gate["executable_prekick_strict"] > 0:
            return {"classification": "CLOSING_PRICE_PENDING", "detail": counts}
        if gate["executable_prekick_strict"] == 0:
            return {"classification": "STRICT_EXECUTABILITY_SCARCITY", "detail": counts}
        return {"classification": "STRICT_SAMPLE_PROGRESSING", "detail": counts}

    delta = compare_checkpoints(previous, current)
    gate_delta = delta["by_gate"][f"{int(PRIMARY_BOTTLENECK_GATE_S)}s"]
    counts["forward_clean_signals_added"] = delta["new_forward_clean_signals"]
    counts["strict_executable_signals_added"] = gate_delta["new_strict_executable_prekick"]
    counts["strict_clv_n_delta"] = gate_delta["new_strict_clv_samples"]

    if gate_delta["new_strict_clv_samples"] > 0:
        return {"classification": "STRICT_SAMPLE_PROGRESSING", "detail": counts}
    if gate["pipeline_failure_candidates"]:
        return {"classification": "CLOSING_PIPELINE_FAILURE", "detail": counts}
    if gate_delta["new_strict_executable_prekick"] > 0:
        return {"classification": "CLOSING_PRICE_PENDING", "detail": counts}
    if delta["new_forward_clean_signals"] > 0 and gate_delta["new_strict_executable_prekick"] <= 0:
        return {"classification": "STRICT_EXECUTABILITY_SCARCITY", "detail": counts}
    return {"classification": "STRICT_EXECUTABILITY_SCARCITY", "detail": counts}


def check_stalled(checkpoint_history: list[dict], gate_s: float = PRIMARY_BOTTLENECK_GATE_S) -> dict | None:
    """Task: 'if strict 45s n does not increase across two completed
    eligible runs, emit STRICT_SAMPLE_STALLED'. Needs at least 3 checkpoints
    (two completed intervals) to evaluate. Returns None if there isn't
    enough history yet, or if the sample grew."""
    if len(checkpoint_history) < 3:
        return None
    key = f"{int(gate_s)}s"
    ns = [cp["lead_gates"][key]["strict_executable_forward_clv_n"] for cp in checkpoint_history[-3:]]
    if ns[-1] > ns[0]:
        return None
    cause = classify_bottleneck(checkpoint_history[-1], checkpoint_history[-2])
    return {
        "stalled": True,
        "gate_s": gate_s,
        "n_history": ns,
        "cause": cause["classification"],
        "cause_detail": cause["detail"],
    }


# ------------------------------------------------------------- kill criterion

PRIMARY_LEAD_S = 45.0
KILL_MIN_N = 150
KILL_MAX_AVG_CLV_PCT = -1.0
DIRECTIONAL_MIN_N = 50


def _clustered_bootstrap_ci(rows: list, n_boot: int = 2000, seed: int = 1234) -> dict | None:
    """Match-clustered bootstrap: resamples MATCH GROUPS with replacement,
    not individual rows -- multiple selections/decisions on the same match
    are correlated and must not be treated as independent observations, per
    the pre-registered kill-criterion's confidence-interval requirement.
    This is additive to (never replaces) strict_forward_clv's own per-row
    bootstrap, which remains an unclustered point-estimate convenience."""
    by_match: dict[int, list[float]] = {}
    for s, c, _p in rows:
        clv_pct = strict_forward_metrics._clv_triple(s["current_decimal"], c.close_price_decimal)["decimal_clv_pct"]
        by_match.setdefault(s["match_id"], []).append(clv_pct)
    match_ids = list(by_match.keys())
    if not match_ids:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sampled = [match_ids[rng.randrange(len(match_ids))] for _ in match_ids]
        vals = [v for mid in sampled for v in by_match[mid]]
        if vals:
            means.append(sum(vals) / len(vals))
    if not means:
        return None
    means.sort()
    return {
        "lower_95": round(means[int(0.025 * len(means))], 3),
        "upper_95": round(means[int(0.975 * len(means))], 3),
        "cluster_unit": "match_id",
        "n_clusters": len(match_ids),
        "n_bootstrap_iterations": n_boot,
    }


def evaluate_thesis_status(db: Session, cross_tab_reconciled: bool) -> dict:
    """Docs/DECISIONS.md pre-registered kill criterion, evaluated read-only.
    Reuses strict_forward_metrics._strict_forward_pairs (row selection) and
    strict_forward_clv (headline n/avg) -- adds only the clustered CI and
    the decision-status mapping."""
    pairs = strict_forward_metrics._strict_forward_pairs(db, PRIMARY_LEAD_S)
    rows = pairs["rows"]
    official = strict_forward_metrics.strict_forward_clv(db, PRIMARY_LEAD_S)
    n = official["strict_executable_forward_clv_n"]
    avg_clv = official["avg_decimal_clv_pct"]
    ci = _clustered_bootstrap_ci(rows) if n > 0 else None

    kill_fires = bool(
        n >= KILL_MIN_N and avg_clv is not None and avg_clv <= KILL_MAX_AVG_CLV_PCT
        and ci is not None and ci["upper_95"] < 0 and cross_tab_reconciled)

    if kill_fires:
        status = "THESIS_KILL_REVIEW_REQUIRED"
    elif n < DIRECTIONAL_MIN_N:
        status = "INSUFFICIENT_EVIDENCE"
    elif avg_clv is not None and avg_clv >= 0:
        status = "DIRECTIONAL_RECOVERY_CANDIDATE"
    elif avg_clv is not None and avg_clv < 0:
        status = "NEGATIVE_DIRECTIONAL_SIGNAL"
    else:
        status = "NO_DEMONSTRATED_EDGE"

    return {
        "primary_lead_time_gate_s": PRIMARY_LEAD_S,
        "n": n,
        "avg_decimal_clv_pct": avg_clv,
        "clustered_bootstrap_ci_95pct": ci,
        "cross_tab_reconciled": cross_tab_reconciled,
        "kill_criterion_n_gate": KILL_MIN_N,
        "kill_criterion_avg_clv_gate_pct": KILL_MAX_AVG_CLV_PCT,
        "kill_criterion_fires": kill_fires,
        "thesis_status": status,
        "progress_to_n50": round(min(n, 50) / 50, 3),
        "progress_to_n150": round(min(n, 150) / 150, 3),
    }
