#!/usr/bin/env python3
"""v0.3.7C: Auto Paper Simulation Runner. PAPER ONLY -- no live betting, no
bet placement automation anywhere in this script or anything it calls.

Writes notes/simulations/YYYY-MM-DD-paper-sim.md,
notes/simulations/latest_paper_sim.json, and appends one row per run to
notes/simulations/simulation_history.csv.

Composes existing, already-tested engines (paper_trade, winner_edge,
execution_classifier_v2, entry_floor_diagnostics, clv_forward_readiness,
market_availability) rather than re-deriving their logic. Historical
provider-time-only results are always labeled DEGRADED; forward results
are labeled CLEAN only when the underlying rows carry real system
timestamps.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engines import (closing_records, collection_evidence, daily_recommendation,  # noqa: E402
                         entry_floor_diagnostics, execution_classifier_v2, market_availability,
                         paper_trade, profit_gates, spot_check_readiness,
                         strict_forward_metrics, verdict_hierarchy, winner_edge)
from app.models import ExecutionClassification, FriendPick, PaperTrade  # noqa: E402

SIM_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/simulations")


def _fetch_health(db, timeout_s: float = 2.0) -> dict:
    """Prefer the real backend's HTTP endpoint -- see the v0.3.7C bug note
    in run_workday_autopilot.py: an in-process health() call in this
    script's own process always sees a never-started collector, regardless
    of the real (separate) backend process's actual state."""
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8000/api/ops/health", timeout=timeout_s)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    from app.routers.ops import health
    return health(db=db)


HISTORY_CSV = SIM_DIR / "simulation_history.csv"
FRIEND_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/friend_picks.csv")

N_NOT_ENOUGH = 50
N_DIRECTIONAL = 50
N_EVIDENCE = 150
N_DECISION = 400
N_ROI_DESCRIPTIVE = 300

VERDICTS = ("NOT ENOUGH DATA", "DATA QUALITY BLOCKED", "EXECUTION BLOCKED", "SOURCE/FEED BLOCKED",
           "SIGNAL TIMING BLOCKED", "FORWARD SAMPLE NON-EXECUTABLE",
           "MODEL UNDERPERFORMS BASELINE", "MODEL SHOWS DIRECTIONAL CLV ONLY",
           "MODEL SHOWS CLEAN FORWARD EDGE CANDIDATE")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _longest_losing_streak(trades) -> int:
    settled = sorted([t for t in trades if t.settlement_status == "SETTLED" and t.paper_pl_usd is not None],
                     key=lambda t: t.created_at)
    longest = current = 0
    for t in settled:
        if t.paper_pl_usd < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


# ------------------------------------------------------------- A. historical replay

def historical_replay(db) -> dict:
    """v0.3.7D.1 partition fix: this used to run over ALL MODEL paper trades
    unconditionally labeled DEGRADED -- the exact same partition-leak
    pattern already fixed in clv_forward_readiness.historical_clv_report()
    (proven on real data: notes/triage/v0_3_7D1-partition-audit.json,
    forward_rows_mislabeled_historical_under_current_code). Now restricted
    to genuinely historical-degraded predictions only, via
    strict_forward_metrics.partition_model_prediction_ids() -- a prediction
    counts as forward-trustworthy if ANY of its delay-bucket trades are
    forward-trustworthy, so it can never appear in both partitions.
    Execution-state distribution uses a fresh, read-only recompute
    (classify_paper_trade) rather than the writing, stale-prone
    classify_all() the old code called here."""
    partition = strict_forward_metrics.partition_model_prediction_ids(db)
    historical_ids = partition["historical_prediction_ids"]

    all_model_trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_source == "MODEL")).all()
    model_trades = [t for t in all_model_trades if t.signal_id in historical_ids]

    all_samples = winner_edge._model_samples(db)
    samples = [s for s in all_samples if s["prediction_id"] in historical_ids]
    scored_samples = [s for s in samples if s["scored"]]
    n = len(scored_samples)
    w = [s["winner_correct"] for s in scored_samples if s["winner_correct"] is not None]
    f = [s["favorite_correct"] for s in scored_samples if s["favorite_correct"] is not None]
    winner_acc = round(100 * sum(w) / len(w), 1) if w else None
    fav_acc = round(100 * sum(f) / len(f), 1) if f else None
    margin = round(winner_acc - fav_acc, 1) if winner_acc is not None and fav_acc is not None else None

    floor_diag = entry_floor_diagnostics.run(db)
    risk = profit_gates.risk_gate(db)

    settled = [t for t in model_trades if t.settlement_status == "SETTLED"]
    filled_or_settled = [t for t in model_trades if t.settlement_status in ("FILLED", "SETTLED")]
    win_count = sum(1 for t in settled if (t.paper_pl_usd or 0) > 0)
    draw_exposure = sum(1 for t in model_trades if t.selection == "draw")

    execution = winner_edge._delay_execution_metrics(model_trades)
    state_dist: dict[str, int] = {}
    for t in model_trades:
        primary, _flags, _degraded, _executability = execution_classifier_v2.classify_paper_trade(db, t)
        state_dist[primary] = state_dist.get(primary, 0) + 1

    return {
        "label": "DEGRADED (provider-time historical rows) -- v0.3.7D.1 partition-fixed: "
                 "forward-trustworthy predictions excluded",
        "eligible_signals": len(samples),
        "distinct_samples": n,
        "filled_trades": len(filled_or_settled),
        "fill_rate_pct": execution.get("30", {}).get("fill_rate_pct"),
        "realized_paper_roi_by_delay": winner_edge._roi_by_delay(db, model_trades),
        "avg_odds_taken": (round(sum(t.price_decimal for t in filled_or_settled if t.price_decimal) /
                                 len([t for t in filled_or_settled if t.price_decimal]), 3)
                          if any(t.price_decimal for t in filled_or_settled) else None),
        "win_rate_pct": round(100 * win_count / len(settled), 1) if settled else None,
        "draw_exposure_count": draw_exposure,
        "max_drawdown_units": risk.get("max_drawdown_units"),
        "longest_losing_streak": _longest_losing_streak(model_trades),
        "market_baseline_winner_accuracy_pct": fav_acc,
        "current_vs_market_baseline_margin_pts": margin,
        "entry_floor_whatif": floor_diag["whatif_lower_floor_simulation"],
        "execution_state_distribution": state_dist,
        "roi_descriptive_only": True,
        "sample_grade": "DEGRADED -- DESCRIPTIVE ONLY (provider-time, not decisional at any sample size)",
        "partition_audit": {
            "total_model_predictions_all_eras": len(all_samples),
            "historical_degraded_predictions": len(historical_ids),
            "forward_trustworthy_predictions_excluded": len(partition["forward_prediction_ids"]),
        },
    }


# ------------------------------------------------------------- B. forward clean

def forward_clean(db) -> dict:
    """v0.3.7D/D.1: separates forward-trustworthy (real system-timestamped)
    rows by STRICT (no-hindsight) executability, not just by primary_state.
    EXECUTABLE_VIA_START_DELAY is reported separately and NEVER folded into
    `executable_n` -- see notes/triage/v0_3_7D1-self-challenge.md Q6:
    counting a signal as executable only because actual kickoff happened
    to run late is a hindsight construction a real-time trader could not
    have relied on."""
    rows = db.scalars(select(ExecutionClassification).where(
        ExecutionClassification.is_historical_degraded.is_(False))).all()
    executable_n = sum(1 for r in rows if r.executability_label == execution_classifier_v2.EXECUTABLE_PREKICK_STRICT)
    via_delay_n = sum(1 for r in rows if r.executability_label == execution_classifier_v2.EXECUTABLE_VIA_START_DELAY)
    research_only_n = sum(1 for r in rows if r.executability_label == execution_classifier_v2.RESEARCH_ONLY_KICKOFF)
    late_n = sum(1 for r in rows if r.executability_label == execution_classifier_v2.LATE_SIGNAL)
    unknown_start_n = sum(1 for r in rows if r.executability_label == execution_classifier_v2.UNKNOWN_START_TIME)
    return {
        "label": "CLEAN (system-timestamped forward rows only)" if rows else "PENDING (0 forward rows yet)",
        "n": len(rows),
        "executable_n": executable_n,
        "executable_via_start_delay_n": via_delay_n,
        "research_only_kickoff_n": research_only_n,
        "late_signal_n": late_n,
        "unknown_start_time_n": unknown_start_n,
        "by_primary_state": {s: sum(1 for r in rows if r.primary_state == s)
                             for s in set(r.primary_state for r in rows)} if rows else {},
    }


# ------------------------------------------------------------- C. CLV-first

def clv_first(db) -> dict:
    from app.engines import clv_forward_readiness
    historical = clv_forward_readiness.historical_clv_report(db)
    forward = clv_forward_readiness.forward_clv_readiness(db)
    return {"historical_provider_time": historical, "forward_system_time": forward}


# ------------------------------------------------------------- D. entry timing

def entry_timing(db) -> dict:
    out = {}
    for delay in paper_trade.DELAYS_SECONDS:
        trades = db.scalars(select(PaperTrade).where(
            PaperTrade.signal_source == "MODEL", PaperTrade.delay_seconds == delay)).all()
        n = len(trades)
        if n == 0:
            out[str(delay)] = {"n": 0}
            continue
        no_data = sum(1 for t in trades if t.settlement_status == "MISSED_PRICE" and t.price_decimal is None)
        below_floor = sum(1 for t in trades if t.settlement_status == "MISSED_PRICE" and t.price_decimal is not None)
        filled = sum(1 for t in trades if t.settlement_status in ("FILLED", "SETTLED"))
        roi = winner_edge._roi_by_delay(db, trades).get(str(delay))
        out[str(delay)] = {
            "n": n,
            "fill_rate_pct": round(100 * filled / n, 1),
            "no_data_rate_pct": round(100 * no_data / n, 1),
            "price_below_floor_rate_pct": round(100 * below_floor / n, 1),
            "roi_pct": roi if filled >= N_ROI_DESCRIPTIVE else None,
            "roi_descriptive_only": filled < N_ROI_DESCRIPTIVE,
        }
    return out


# ------------------------------------------------------------- E. market availability

def market_availability_sim(db) -> dict:
    return market_availability.prevalence_report(db)


# ------------------------------------------------------------- F. friend shadow

def friend_shadow(db) -> dict:
    if not FRIEND_CSV.exists():
        return {"clean_n": 0, "note": "no friend_picks.csv found"}
    with open(FRIEND_CSV) as f:
        rows = list(csv.DictReader(f))
    clean = [r for r in rows
            if r.get("clean_scored") == "TRUE" and r.get("price_at_receipt") and r.get("book")
            and r.get("market_type") and r.get("logged_after_result") != "TRUE"]
    groups: dict[str, list] = {}
    for r in clean:
        groups.setdefault(r.get("signal_group_id", ""), []).append(r)
    return {
        "clean_n": len(clean),
        "retro_excluded_n": sum(1 for r in rows if r.get("logged_after_result") == "TRUE"),
        "correlated_leg_groups_n": sum(1 for g in groups.values() if len(g) > 1),
        "note": "retro/result-known picks are excluded from clean sim and listed only as coverage evidence.",
    }


# ------------------------------------------------------------- G. strict forward (v0.3.7D.1)

def strict_forward_section(db) -> dict:
    """v0.3.7D.1 Tasks 2/3/4/5: the reconciled executability x primary-state
    cross-tab, strict executable-forward CLV at each lead-time gate, and the
    paired CurrentModel-vs-MarketBaseline comparison -- all on the strict,
    no-hindsight, forward-clean subset. This is the section the whole
    release exists to add; everything else in this file is unchanged
    historical/diagnostic reporting."""
    cross_tab = strict_forward_metrics.forward_executability_primary_state_cross_tab(db)
    clv_all_gates = strict_forward_metrics.strict_forward_clv_all_gates(db)
    paired_20s = strict_forward_metrics.paired_market_baseline_comparison(db, lead_s=20.0)
    return {"cross_tab": cross_tab, "strict_clv_by_lead_gate": clv_all_gates,
           "paired_baseline_comparison_20s": paired_20s}


# ------------------------------------------------------------- H. spot-check readiness (v0.3.7D.1)

def spot_check_section(db) -> dict:
    return spot_check_readiness.spot_check_readiness_report(db)


# ------------------------------------------------------------- I. deterministic verdict hierarchy (v0.3.7D.1)

def verdict_hierarchy_section(db, g: dict, health: dict, evidence: dict) -> dict:
    """v0.3.7D.1 Task 7 / v0.3.7D.2 fix: the new 10-branch deterministic
    verdict, computed alongside (not replacing) the pre-existing
    `final_verdict()` legacy string verdict below -- both are reported;
    this is the decisional one per this release's hard rules.

    v0.3.7D.2: `collection_has_run` now comes from
    collection_evidence.resolve_collection_evidence() rather than checking
    only the new (D.1) Settings.last_completed_run_* bookkeeping directly --
    that bookkeeping is NULL for any run that completed under pre-D.1 code,
    which produced a false COLLECTION_NOT_RUN verdict on real, reconciled
    data. See notes/triage/v0_3_7D2-daily-cycle-integration-fix.md.

    v0.3.7D.3: `evidence` is now resolved once by the caller and passed in,
    so this verdict and daily_recommendation.build_recommendation() below
    are guaranteed to reason from the identical evidence object -- see
    notes/triage/v0_3_7D3-recommendation-consistency.md."""
    active_window = bool(health.get("expected_collection_window_active", True))
    verdict = verdict_hierarchy.determine_verdict(
        collection_has_run=evidence["collection_has_run"],
        active_collection_window=active_window,
        cross_tab=g["cross_tab"],
        strict_clv=g["strict_clv_by_lead_gate"]["lead_20s"],
        paired=g["paired_baseline_comparison_20s"])
    verdict["collection_has_run"] = evidence["collection_has_run"]
    verdict["collection_run_evidence_source"] = evidence["evidence_source"]
    verdict["collection_run_evidence_detail"] = evidence["detail"]
    return verdict


def _gate_label(n: int) -> str:
    if n < N_NOT_ENOUGH:
        return "NOT ENOUGH DATA"
    if n < N_EVIDENCE:
        return "DIRECTIONAL"
    if n < N_DECISION:
        return "EVIDENCE"
    return "DECISION-GRADE"


def final_verdict(a: dict, b: dict, health_status: str) -> str:
    if health_status == "FAIL":
        return "SOURCE/FEED BLOCKED"
    n = a["distinct_samples"]
    if n < N_NOT_ENOUGH:
        return "NOT ENOUGH DATA"

    # v0.3.7D: a forward-trustworthy sample that is entirely non-executable
    # must never fall through to a model-vs-baseline verdict -- see
    # notes/triage/v0_3_7D-signal-timing-audit.md. Checked BEFORE the
    # execution/baseline checks below, which operate on the historical
    # (degraded) sample and would otherwise overclaim.
    if b["n"] > 0 and b["executable_n"] == 0:
        return "SIGNAL TIMING BLOCKED"
    if b["n"] > 0 and 0 < b["executable_n"] < N_NOT_ENOUGH:
        return "FORWARD SAMPLE NON-EXECUTABLE"

    exec_dist = a["execution_state_distribution"]
    total = sum(exec_dist.values()) or 1
    if exec_dist.get("NO_DATA_AT_ENTRY", 0) / total > 0.7:
        return "EXECUTION BLOCKED"
    margin = a["current_vs_market_baseline_margin_pts"]
    if margin is not None and margin < 0:
        return "MODEL UNDERPERFORMS BASELINE"
    if b["n"] < N_EVIDENCE:
        return "MODEL SHOWS DIRECTIONAL CLV ONLY"
    return "MODEL SHOWS CLEAN FORWARD EDGE CANDIDATE"


def build_report(db=None) -> dict:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        h = _fetch_health(db)
        a = historical_replay(db)
        b = forward_clean(db)
        c = clv_first(db)
        d = entry_timing(db)
        e = market_availability_sim(db)
        f = friend_shadow(db)
        g = strict_forward_section(db)
        spot = spot_check_section(db)
        verdict = final_verdict(a, b, h["status"])
        now = _now()
        evidence = collection_evidence.resolve_collection_evidence(db, h, now)
        verdict_v2 = verdict_hierarchy_section(db, g, h, evidence)
        recommendation = daily_recommendation.build_recommendation(
            db, h, now=now, evidence=evidence, cross_tab=g["cross_tab"],
            strict_clv=g["strict_clv_by_lead_gate"]["lead_20s"],
            paired=g["paired_baseline_comparison_20s"])
        evidence_consistency = daily_recommendation.check_evidence_consistency(verdict_v2, recommendation)
        return {
            "date": _now().strftime("%Y-%m-%d"), "generated_at": _now().isoformat(),
            "health_status": h["status"],
            "data_scope": {
                "historical_eligible_signals": a["eligible_signals"],
                "historical_distinct_samples": a["distinct_samples"],
                "forward_clean_n": b["n"],
                "gate": _gate_label(a["distinct_samples"]),
            },
            "a_historical_replay": a, "b_forward_clean": b, "c_clv_first": c,
            "d_entry_timing": d, "e_market_availability": e, "f_friend_shadow": f,
            "g_strict_forward": g, "h_spot_check_readiness": spot,
            "self_challenge": {
                "what_could_be_wrong": "Historical numbers are DEGRADED (provider-time only) -- any ROI/CLV "
                                      "here is a non-executable proxy, not evidence of real tradeable edge.",
                "what_would_reverse_this": "Forward (CLEAN) data reaching n>=50 could show a materially "
                                          "different picture than the historical DEGRADED numbers.",
            },
            "final_verdict": verdict,
            "verdict_hierarchy": verdict_v2,
            "daily_recommendation": recommendation,
            "evidence_consistency": evidence_consistency,
        }
    finally:
        if owns_session:
            db.close()


def render_markdown(r: dict) -> str:
    lines = [
        f"# Paper Simulation — {r['date']}", "", f"Generated: {r['generated_at']}",
        f"Health status: {r['health_status']}", "",
        "## 1. Data scope", f"```json\n{json.dumps(r['data_scope'], indent=2)}\n```", "",
        "## 2. Strategy comparison (historical replay, DEGRADED)",
        f"```json\n{json.dumps(r['a_historical_replay'], indent=2, default=str)}\n```", "",
        "## 2b. Forward clean simulation",
        f"```json\n{json.dumps(r['b_forward_clean'], indent=2)}\n```", "",
        "## 2c. CLV-first", f"```json\n{json.dumps(r['c_clv_first'], indent=2, default=str)}\n```", "",
        "## 3. Delay comparison table", f"```json\n{json.dumps(r['d_entry_timing'], indent=2)}\n```", "",
        "## Market availability", f"```json\n{json.dumps(r['e_market_availability'], indent=2)}\n```", "",
        "## Friend shadow simulation", f"```json\n{json.dumps(r['f_friend_shadow'], indent=2)}\n```", "",
        "## 4. Execution failure breakdown",
        f"```json\n{json.dumps(r['a_historical_replay']['execution_state_distribution'], indent=2)}\n```", "",
        "## 5. Self-challenge", f"```json\n{json.dumps(r['self_challenge'], indent=2)}\n```", "",
        "## 6. Final daily simulation verdict (legacy)", f"**{r['final_verdict']}**", "",
        "## 7. Strict forward: cross-tab, CLV-by-lead-gate, paired baseline (v0.3.7D.1)",
        f"```json\n{json.dumps(r['g_strict_forward'], indent=2, default=str)}\n```", "",
        "## 8. Spot-check / placeability readiness (coverage evidence, not a gate)",
        f"**{r['h_spot_check_readiness']['label']}**",
        f"```json\n{json.dumps(r['h_spot_check_readiness'], indent=2)}\n```", "",
        "## 9. Deterministic verdict hierarchy (v0.3.7D.1, decisional)",
        f"**{r['verdict_hierarchy']['verdict']}**",
        f"```json\n{json.dumps(r['verdict_hierarchy'], indent=2)}\n```", "",
        "## 10. Daily recommendation",
        f"**{r['daily_recommendation']['message']}**",
        f"```json\n{json.dumps(r['daily_recommendation'], indent=2, default=str)}\n```", "",
        "## 11. Verdict/recommendation evidence consistency",
        f"**{'CONSISTENT' if r['evidence_consistency']['consistent'] else r['evidence_consistency']['flag']}**",
        f"```json\n{json.dumps(r['evidence_consistency'], indent=2, default=str)}\n```",
    ]
    return "\n".join(lines)


def append_history(r: dict):
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not HISTORY_CSV.exists()
    with open(HISTORY_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["date", "generated_at", "health_status", "eligible_signals", "forward_clean_n",
                       "gate", "final_verdict"])
        w.writerow([r["date"], r["generated_at"], r["health_status"], r["data_scope"]["historical_eligible_signals"],
                   r["data_scope"]["forward_clean_n"], r["data_scope"]["gate"], r["final_verdict"]])


def main():
    r = build_report()
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    md_path = SIM_DIR / f"{r['date']}-paper-sim.md"
    json_path = SIM_DIR / "latest_paper_sim.json"
    md_path.write_text(render_markdown(r))
    json_path.write_text(json.dumps(r, indent=2, default=str))
    append_history(r)
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Appended {HISTORY_CSV}")
    print(f"Final verdict: {r['final_verdict']}")
    assert r["final_verdict"] in VERDICTS
    if not r["evidence_consistency"]["consistent"]:
        print(f"FAIL: RECOMMENDATION_EVIDENCE_MISMATCH -- {r['evidence_consistency']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
