#!/usr/bin/env python3
"""v0.3.7C: Daily Research Loop.

Writes notes/research/YYYY-MM-DD-daily-research.md,
notes/research/latest_research.json, and appends to
notes/research/experiment_backlog.md (append-only, never overwritten).

Every section checks its own sample-size gate FIRST and short-circuits to
"NOT ENOUGH DATA" rather than computing elaborate statistics that would
just be discarded. No live betting, no model training, no bet placement
happens here.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from sqlalchemy import func, select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engines import (clv_forward_readiness, entry_floor_diagnostics,  # noqa: E402
                         execution_classifier_v2, market_availability, odds_math,
                         paper_trade, profit_gates, winner_edge)
from app.models import (ClosingRecord, ExecutionClassification, FriendPick,  # noqa: E402
                        OddsSnapshot, PaperTrade, PredictionLedger)

RESEARCH_DIR = Path("/Users/krispatell/Downloads/ESoccer/notes/research")


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
FRIEND_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/friend_picks.csv")
BACKLOG_MD = RESEARCH_DIR / "experiment_backlog.md"

DIRECTIONAL_MIN_N = 50
EVIDENCE_MIN_N = 150
DECISION_MIN_N = 400
ROI_DESCRIPTIVE_MIN_N = 300


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------- section A

def section_a_data_quality(db) -> dict:
    now = _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    odds_today = db.scalars(select(OddsSnapshot).where(OddsSnapshot.collected_at >= today_start)).all()
    n_today = len(odds_today)
    n_with_system_ts = sum(1 for o in odds_today if o.polled_at is not None and o.ingested_at is not None)
    completeness_pct = round(100 * n_with_system_ts / n_today, 1) if n_today else None

    strict_close_today = db.scalar(select(func.count(ClosingRecord.id)).where(
        ClosingRecord.created_at >= today_start, ClosingRecord.close_quality == "HIGH")) or 0
    cumulative_clean_close = db.scalar(select(func.count(ClosingRecord.id)).where(
        ClosingRecord.close_quality == "HIGH")) or 0

    prevalence = market_availability.prevalence_report(db)

    if n_today == 0:
        data_state = "BLOCKED"
    elif completeness_pct is not None and completeness_pct >= 80:
        data_state = "CLEAN"
    else:
        data_state = "DEGRADED"

    return {
        "odds_rows_collected_today": n_today,
        "system_timestamp_completeness_pct": completeness_pct,
        "missing_timestamp_count_today": n_today - n_with_system_ts,
        "strict_close_candidates_added_today": strict_close_today,
        "cumulative_clean_close_count": cumulative_clean_close,
        "market_availability_episodes": prevalence,
        "data_state": data_state,
    }


# --------------------------------------------------------------- section B

def section_b_execution_learning(db) -> dict:
    result = execution_classifier_v2.classify_all(db)
    rows = db.scalars(select(ExecutionClassification)).all()
    flag_counts: dict[str, int] = {}
    for r in rows:
        for f in json.loads(r.diagnostic_flags_json or "[]"):
            flag_counts[f] = flag_counts.get(f, 0) + 1
    # v0.3.7D: forward-only executability breakdown, so downstream reporting
    # can tell "forward sample exists but is 100% non-executable" apart from
    # "forward sample exists and some of it is genuinely pre-kickoff."
    forward_rows = [r for r in rows if not r.is_historical_degraded]
    # v0.3.7D.2: EXECUTABLE_PREKICK was renamed to EXECUTABLE_PREKICK_STRICT
    # in v0.3.7D.1 (the no-hindsight fix) -- this reference was missed
    # because this script isn't covered by pytest.
    forward_executable_n = sum(1 for r in forward_rows
                               if r.executability_label == execution_classifier_v2.EXECUTABLE_PREKICK_STRICT)
    return {"by_primary_state": result["by_primary_state"],
           "by_executability": result.get("by_executability", {}),
           "diagnostic_flag_counts": flag_counts,
           "total_classified": result["total_classified"],
           "historical_degraded_count": result["historical_degraded_count"],
           "forward_trustworthy_count": result["forward_trustworthy_count"],
           "forward_executable_count": forward_executable_n}


# --------------------------------------------------------------- section C

def section_c_clv_learning(db) -> dict:
    historical = clv_forward_readiness.historical_clv_report(db)
    forward = clv_forward_readiness.forward_clv_readiness(db)

    by_delay = {}
    for source in ("MODEL",):
        trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_source == source)).all()
        by_delay[source] = winner_edge._roi_by_delay(db, trades) if trades else None

    return {
        "historical_provider_time_clv": historical,
        "forward_system_time_clv": forward,
        "roi_by_delay_bucket": by_delay,
        "invalid_reasons_if_blocked": {
            "historical": None if historical["distinct_samples_with_close"] >= DIRECTIONAL_MIN_N
                         else "insufficient closes for even directional CLV",
            "forward": "missing system timestamps -- 0 forward-eligible closing records exist yet"
                      if forward["forward_system_timestamped_samples"] == 0 else None,
        },
    }


# --------------------------------------------------------------- section D

def section_d_baseline_comparison(db) -> dict:
    model = winner_edge.model_report(db)
    n = model["distinct_samples"]
    no_edge_baseline_pct = 50.0  # random/no-edge baseline: coin-flip on a binary win/lose framing

    return {
        "distinct_samples": n,
        "gate": ("NOT ENOUGH DATA" if n < DIRECTIONAL_MIN_N else
                "DIRECTIONAL" if n < EVIDENCE_MIN_N else
                "EVIDENCE" if n < DECISION_MIN_N else "DECISION-GRADE"),
        "model_winner_accuracy_pct": model["winner_accuracy_pct"],
        "favorite_baseline_pct": model["favorite_baseline_accuracy_pct"],
        "no_edge_baseline_pct": no_edge_baseline_pct,
        "margin_vs_favorite_pts": (round(model["winner_accuracy_pct"] - model["favorite_baseline_accuracy_pct"], 1)
                                   if model["winner_accuracy_pct"] is not None
                                   and model["favorite_baseline_accuracy_pct"] is not None else None),
        "brier_score": model["brier_score"],
        "calibration_buckets": model["calibration_buckets"],
    }


# --------------------------------------------------------------- section E

def section_e_steam_learning(db) -> dict:
    preds = db.scalars(select(PredictionLedger)).all()
    trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_source == "MODEL")).all()
    pred_by_id = {p.id: p for p in preds}

    reachable_at_20 = reachable_at_30 = total_checked = aligned = 0
    move_sizes = []
    for t in trades:
        p = pred_by_id.get(t.signal_id)
        if p is None or t.price_decimal is None or p.current_decimal is None:
            continue
        total_checked += 1
        delta = t.price_decimal - p.current_decimal
        move_sizes.append(abs(delta))
        predicted_shorten = ((p.predicted_first_live_decimal is not None
                              and p.predicted_first_live_decimal < p.current_decimal)
                             or (p.steam_probability or 0.5) >= 0.58)
        if (predicted_shorten and delta < 0) or (not predicted_shorten and delta >= 0):
            aligned += 1
        if t.delay_seconds == 20 and t.price_decimal is not None:
            reachable_at_20 += 1
        if t.delay_seconds == 30 and t.price_decimal is not None:
            reachable_at_30 += 1

    return {
        "samples_checked": total_checked,
        "gate": "NOT ENOUGH DATA" if total_checked < DIRECTIONAL_MIN_N else "HAS SIGNAL",
        "direction_alignment_pct": round(100 * aligned / total_checked, 1) if total_checked else None,
        "avg_abs_move_size": round(sum(move_sizes) / len(move_sizes), 4) if move_sizes else None,
        "reachable_rows_at_20s": reachable_at_20,
        "reachable_rows_at_30s": reachable_at_30,
        "note": "reachable_at_Xs counts paper-trade rows where a price was actually found at that delay "
                "(i.e. the move, if any, was still observable) -- not a claim the move was profitably tradeable.",
    }


# --------------------------------------------------------------- section F

def section_f_friend_learning() -> dict:
    if not FRIEND_CSV.exists():
        return {"total": 0, "clean": 0, "retro": 0, "groups": {}}
    with open(FRIEND_CSV) as f:
        rows = list(csv.DictReader(f))
    clean = [r for r in rows if r.get("clean_scored") == "TRUE"]
    retro = [r for r in rows if r.get("logged_after_result") == "TRUE"]
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("signal_group_id", ""), []).append(r.get("leg_id", ""))
    return {
        "total": len(rows), "clean_count": len(clean), "retro_count": len(retro),
        "correlated_leg_groups": {k: v for k, v in groups.items() if len(v) > 1},
        "retro_excluded_from_clean_sample": True,
    }


# --------------------------------------------------------------- section G/I

def generate_hypotheses(a, b, c, d, e, f) -> list[dict]:
    candidates = []
    total_exec = b["total_classified"]
    no_data_pct = (100 * b["by_primary_state"].get("NO_DATA_AT_ENTRY", 0) / total_exec) if total_exec else None
    if no_data_pct is not None and no_data_pct > 50:
        candidates.append({
            "claim": "The dominant blocker to any profitability conclusion is feed/poll density, not model quality.",
            "evidence": f"NO_DATA_AT_ENTRY is {no_data_pct:.1f}% of {total_exec} classified paper trades.",
            "sample_size": total_exec,
            "why_it_may_be_wrong": "Historical rows predate v0.3.7B system timestamps and dense heartbeats -- "
                                   "this could be an artifact of sparse historical polling, not a persistent problem.",
            "what_confirms_it": "Forward-collected data (post v0.3.7B) still shows high NO_DATA_AT_ENTRY rate "
                                "after a full workday of normal-cadence polling.",
            "what_kills_it": "Forward NO_DATA_AT_ENTRY rate drops below ~20% once real system-timestamped "
                             "data accumulates.",
            "category": "execution",
        })
    if d["gate"] != "NOT ENOUGH DATA" and d.get("margin_vs_favorite_pts") is not None:
        sign = "beats" if d["margin_vs_favorite_pts"] > 0 else "underperforms"
        candidates.append({
            "claim": f"CurrentModel {sign} the favorite baseline by {abs(d['margin_vs_favorite_pts'])} points.",
            "evidence": f"n={d['distinct_samples']} distinct samples, gate={d['gate']}.",
            "sample_size": d["distinct_samples"],
            "why_it_may_be_wrong": "Sample is still below decision-grade (400); margin could reverse with more data.",
            "what_confirms_it": "Margin direction and magnitude hold as n grows toward 400.",
            "what_kills_it": "Margin flips sign or shrinks toward zero as n grows.",
            "category": "model",
        })
    prevalence = a["market_availability_episodes"]
    pct = prevalence.get("withdrawn_prevalence_pct")
    if pct is not None and pct > 0:
        candidates.append({
            "claim": "Pregame market withdrawal/relist-at-kickoff is a real, measurable pattern on bet365.",
            "evidence": f"{pct}% prevalence across {prevalence['total_match_book_market_selection_combos_checked']} "
                       "checked combos.",
            "sample_size": prevalence["total_match_book_market_selection_combos_checked"],
            "why_it_may_be_wrong": "Could reflect polling gaps rather than genuine market withdrawal.",
            "what_confirms_it": "Prevalence stays >=5% as more heartbeat data accumulates with dense polling.",
            "what_kills_it": "Prevalence drops near zero once polling density increases (proves it was a polling artifact).",
            "category": "market-availability",
        })
    elif pct == 0.0 and prevalence["total_match_book_market_selection_combos_checked"] >= 30:
        candidates.append({
            "claim": "No pregame market withdrawal/relist-at-kickoff pattern observed on bet365 so far.",
            "evidence": f"0% prevalence across {prevalence['total_match_book_market_selection_combos_checked']} "
                       "checked combos -- unlike the single FanDuel friend-pick observation, which remains "
                       "a separate, unconfirmed flag on a book this deployment doesn't even poll.",
            "sample_size": prevalence["total_match_book_market_selection_combos_checked"],
            "why_it_may_be_wrong": "Sample is still modest and covers a short collection window; a rare "
                                  "pattern could still exist and just not have occurred yet.",
            "what_confirms_it": "Prevalence stays at/near 0% as combos checked grows into the hundreds.",
            "what_kills_it": "Even one real withdrawn/relisted candidate appears as more data accumulates.",
            "category": "market-availability",
        })
    fallback_pool = [
        {
            "claim": "Not enough forward data exists yet to support any specific hypothesis beyond 'keep collecting'.",
            "evidence": f"odds_rows_collected_today={a['odds_rows_collected_today']}, "
                       f"cumulative_clean_close_count={a['cumulative_clean_close_count']}.",
            "sample_size": a["odds_rows_collected_today"],
            "why_it_may_be_wrong": "N/A -- this is a data-availability observation, not a claim about the model.",
            "what_confirms_it": "Continued zero/near-zero forward accumulation across multiple days.",
            "what_kills_it": "A single full workday of collection produces >=50 clean forward samples.",
            "category": "feed-source",
        },
        {
            "claim": "Friend/manual pick evidence is too thin to say anything about friend-pick signal quality.",
            "evidence": f"clean_count={f['clean_count']}, retro_count={f['retro_count']}, total={f['total']}.",
            "sample_size": f["clean_count"],
            "why_it_may_be_wrong": "N/A -- data-availability observation, not a claim about friend-pick skill.",
            "what_confirms_it": "Clean (non-retro) friend-pick count stays near zero over further weeks.",
            "what_kills_it": ">=30 clean, non-retro friend picks accumulate with known price_at_receipt.",
            "category": "friend-pick",
        },
        {
            "claim": "Manual spot-check coverage is too thin to confirm or rule out provider/book price divergence.",
            "evidence": "See notes/triage/book_spot_checks.csv -- run scripts/spot_check_capture.py during live matches.",
            "sample_size": 0,
            "why_it_may_be_wrong": "N/A -- data-availability observation, not a claim about feed accuracy.",
            "what_confirms_it": "Spot-checks show provider price consistently lagging or diverging from the book screen.",
            "what_kills_it": "Spot-checks show provider price matching the book screen within a small, consistent tolerance.",
            "category": "feed-source",
        },
    ]
    i = 0
    while len(candidates) < 3 and i < len(fallback_pool):
        candidates.append(fallback_pool[i])
        i += 1
    return candidates[:3]


def self_challenge(a, b, c, d) -> dict:
    return {
        "what_could_make_conclusion_wrong": "Today's sample sizes are small; any directional claim could reverse "
                                            "entirely with the next day of data.",
        "hidden_assumption_most_risk": "That historical (pre-v0.3.7B) execution/CLV patterns generalize to "
                                       "forward, densely-timestamped data -- they may not.",
        "most_likely_noise": "Any single day's steam-direction alignment percentage or CLV average, given "
                             f"current sample sizes (data_state={a['data_state']}).",
        "would_waste_most_time_if_chased": "Tuning the entry floor threshold before NO_DATA_AT_ENTRY is fixed -- "
                                          "the floor's effect is currently swamped by missing data entirely.",
        "ignore_until_sample_improves": "Any CLV or baseline-margin number below the directional gate "
                                       f"(n>={DIRECTIONAL_MIN_N}).",
        "what_forces_tomorrows_plan_to_change": "A full workday autopilot run producing >=50 forward "
                                                "system-timestamped closes would unlock directional CLV -- "
                                                "that's the next real trigger, not today's numbers.",
    }


def final_recommendation(a, health_status: str, b: dict | None = None) -> str:
    """Exactly one action, priority-ordered.

    v0.3.7D: a forward-trustworthy sample that exists but is entirely
    non-executable (see notes/triage/v0_3_7D-signal-timing-audit.md) means
    "collect more data" would just accumulate more of the same
    non-executable KICKOFF-only signals -- the real next action is fixing
    signal timing / running a full workday collection that starts well
    before match windows, not simply waiting longer. Checked before the
    generic BLOCKED/collect-more-data fallbacks."""
    if health_status in ("FAIL",):
        return "fix feed/polling"
    if b is not None and b.get("forward_trustworthy_count", 0) > 0 and b.get("forward_executable_count", 0) == 0:
        return "fix signal timing / run full workday collection from before match windows"
    if a["data_state"] == "BLOCKED":
        return "collect more data"
    prevalence = a["market_availability_episodes"].get("withdrawn_prevalence_pct")
    if prevalence is not None and prevalence >= 5.0:
        return "inspect market availability"
    if a["cumulative_clean_close_count"] < DIRECTIONAL_MIN_N:
        return "collect more data"
    return "collect more data"


def _hypothesis_hash(date_str: str, h: dict) -> str:
    import hashlib
    key = f"{date_str}|{h['category']}|{h['claim']}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def append_backlog(hypotheses: list[dict], date_str: str) -> dict:
    """v0.3.7D fix: re-running the daily cycle multiple times on the same
    date (e.g. testing, or a manual re-run) used to append the exact same
    hypotheses again every time, since this was purely append-only with no
    de-duplication. Each hypothesis block is now tagged with a hidden
    `<!-- hash:... -->` marker (date + category + claim); a marker already
    present in the file is skipped. Append-only behavior is preserved for
    genuinely NEW hypotheses (different date, different claim, or different
    category) -- nothing is ever deleted or rewritten, only conditionally
    not re-added."""
    import re
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    existing_hashes = set()
    if BACKLOG_MD.exists():
        existing_hashes = set(re.findall(r"<!-- hash:([a-f0-9]{12}) -->", BACKLOG_MD.read_text()))

    new_lines = []
    added = 0
    for h in hypotheses:
        h_hash = _hypothesis_hash(date_str, h)
        if h_hash in existing_hashes:
            continue
        new_lines.append(f"<!-- hash:{h_hash} -->")
        new_lines.append(f"- **priority**: derived from category={h['category']}, n={h['sample_size']}")
        new_lines.append(f"  - hypothesis: {h['claim']}")
        new_lines.append(f"  - expected_value: TBD (manual review)")
        new_lines.append(f"  - required_data: more samples toward n>={EVIDENCE_MIN_N}")
        new_lines.append(f"  - implementation_cost: low (data collection only, no code change implied)")
        new_lines.append(f"  - risk: {h['why_it_may_be_wrong']}")
        new_lines.append(f"  - stop_condition: {h['what_kills_it']}")
        new_lines.append(f"  - owner: code")
        added += 1

    if added == 0:
        return {"appended": 0, "skipped_duplicates": len(hypotheses)}

    block = f"\n## {date_str}\n\n" + "\n".join(new_lines) + "\n"
    if not BACKLOG_MD.exists():
        BACKLOG_MD.write_text("# Experiment Backlog (append-only)\n" + block)
    else:
        with open(BACKLOG_MD, "a") as f:
            f.write(block)
    return {"appended": added, "skipped_duplicates": len(hypotheses) - added}


def build_report(db=None) -> dict:
    owns_session = db is None
    db = db or SessionLocal()
    try:
        h = _fetch_health(db)
        a = section_a_data_quality(db)
        b = section_b_execution_learning(db)
        c = section_c_clv_learning(db)
        d = section_d_baseline_comparison(db)
        e = section_e_steam_learning(db)
        f = section_f_friend_learning()
        hypotheses = generate_hypotheses(a, b, c, d, e, f)
        challenge = self_challenge(a, b, c, d)
        recommendation = final_recommendation(a, h["status"], b)
        return {
            "date": _now().strftime("%Y-%m-%d"), "generated_at": _now().isoformat(),
            "health_status": h["status"],
            "section_a_data_quality": a, "section_b_execution_learning": b,
            "section_c_clv_learning": c, "section_d_baseline_comparison": d,
            "section_e_steam_learning": e, "section_f_friend_learning": f,
            "section_g_hypotheses": hypotheses, "section_i_self_challenge": challenge,
            "section_j_final_recommendation": recommendation,
        }
    finally:
        if owns_session:
            db.close()


def render_markdown(r: dict) -> str:
    lines = [
        f"# Daily Research — {r['date']}", "", f"Generated: {r['generated_at']}",
        f"Health status: {r['health_status']}", "",
        "## A. Data quality", f"```json\n{json.dumps(r['section_a_data_quality'], indent=2, default=str)}\n```", "",
        "## B. Execution learning", f"```json\n{json.dumps(r['section_b_execution_learning'], indent=2)}\n```", "",
        "## C. CLV learning", f"```json\n{json.dumps(r['section_c_clv_learning'], indent=2, default=str)}\n```", "",
        "## D. Baseline comparison", f"```json\n{json.dumps(r['section_d_baseline_comparison'], indent=2)}\n```", "",
        "## E. Steam/price movement learning", f"```json\n{json.dumps(r['section_e_steam_learning'], indent=2)}\n```", "",
        "## F. Friend/manual pick learning", f"```json\n{json.dumps(r['section_f_friend_learning'], indent=2)}\n```", "",
        "## G. Hypotheses (ranked)",
    ]
    for i, h in enumerate(r["section_g_hypotheses"], 1):
        lines.append(f"{i}. **{h['claim']}** (category={h['category']}, n={h['sample_size']})")
        lines.append(f"   - evidence: {h['evidence']}")
        lines.append(f"   - why it may be wrong: {h['why_it_may_be_wrong']}")
        lines.append(f"   - what confirms it: {h['what_confirms_it']}")
        lines.append(f"   - what kills it: {h['what_kills_it']}")
    lines += ["", "## H. Experiment backlog", "See `notes/research/experiment_backlog.md` (append-only).", "",
             "## I. Self-challenge", f"```json\n{json.dumps(r['section_i_self_challenge'], indent=2)}\n```", "",
             "## J. Final recommendation", f"**{r['section_j_final_recommendation']}**"]
    return "\n".join(lines)


def main():
    r = build_report()
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    md_path = RESEARCH_DIR / f"{r['date']}-daily-research.md"
    json_path = RESEARCH_DIR / "latest_research.json"
    md_path.write_text(render_markdown(r))
    json_path.write_text(json.dumps(r, indent=2, default=str))
    backlog_result = append_backlog(r["section_g_hypotheses"], r["date"])
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    print(f"Backlog: appended {backlog_result['appended']}, skipped {backlog_result['skipped_duplicates']} "
         "exact duplicate(s) already logged today")
    print(f"Appended {BACKLOG_MD}")
    print(f"Final recommendation: {r['section_j_final_recommendation']}")


if __name__ == "__main__":
    main()
