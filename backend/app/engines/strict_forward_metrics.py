"""v0.3.7D.1: strict, no-hindsight forward performance metrics.

Answers the one decision-critical question this release exists for: what is
the CLV and execution performance of the strict, genuinely pre-kickoff,
forward-clean, system-timestamped, executable subset -- excluding
historical degraded rows, research-only KICKOFF rows, and rows that only
appear executable because actual kickoff occurred later than scheduled.

Read-only report module. Never mutates PaperTrade, PredictionLedger,
ExecutionClassification, or ClosingRecord rows -- every function here only
SELECTs and computes.
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import execution_classifier_v2 as ecv2
from . import odds_math, winner_edge
from .closing_records import HIGH, MEDIUM
from ..models import ClosingRecord, ExecutionClassification, Match, PaperTrade, PredictionLedger

LEAD_TIME_GATES_S = ecv2.LEAD_TIME_GATES_S  # (20.0, 30.0, 45.0)
VALID_CLOSE_QUALITIES = (HIGH, MEDIUM)

PRIMARY_STATES = (ecv2.FILLED, ecv2.NO_DATA_AT_ENTRY, ecv2.PRICE_BELOW_ENTRY_FLOOR,
                  ecv2.MARKET_UNAVAILABLE_AT_ENTRY, ecv2.BOOK_MISSING_MARKET_STATE,
                  ecv2.SIGNAL_TOO_LATE, ecv2.TIMESTAMP_UNTRUSTWORTHY, ecv2.UNKNOWN)
EXECUTABILITY_CLASSES = (ecv2.EXECUTABLE_PREKICK_STRICT, ecv2.EXECUTABLE_VIA_START_DELAY,
                         ecv2.RESEARCH_ONLY_KICKOFF, ecv2.LATE_SIGNAL, ecv2.UNKNOWN_START_TIME)


def partition_model_prediction_ids(db: Session) -> dict:
    """v0.3.7D.1: a PredictionLedger row's era is determined by whether ANY
    of its associated MODEL PaperTrade rows are forward-trustworthy
    (is_historical_degraded=False on at least one delay-bucket trade).
    Used to fix the partition leak in run_daily_paper_sim.py's
    historical_replay(), which previously reported on ALL MODEL trades
    unconditionally labeled DEGRADED."""
    trades = db.scalars(select(PaperTrade).where(PaperTrade.signal_source == "MODEL")).all()
    classified = {r.paper_trade_id: r.is_historical_degraded for r in
                 db.scalars(select(ExecutionClassification)).all()}
    forward_pred_ids: set[int] = set()
    historical_pred_ids: set[int] = set()
    for t in trades:
        degraded = classified.get(t.id)
        if degraded is None:
            _primary, _flags, degraded, _ex = ecv2.classify_paper_trade(db, t)
        if degraded:
            historical_pred_ids.add(t.signal_id)
        else:
            forward_pred_ids.add(t.signal_id)
    # a prediction with at least one forward-trustworthy trade counts as forward
    historical_only_ids = historical_pred_ids - forward_pred_ids
    return {"forward_prediction_ids": forward_pred_ids, "historical_prediction_ids": historical_only_ids}


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _clv_triple(entry_decimal: float, close_decimal: float) -> dict:
    """Positive = your entry price was better than the eventual close (you
    beat the market). Same sign convention across all three views."""
    decimal_clv_pct = round(odds_math.clv_pct(entry_decimal, close_decimal) * 100, 3)
    entry_p, close_p = odds_math.implied_prob(entry_decimal), odds_math.implied_prob(close_decimal)
    implied_prob_clv_pct = round((close_p - entry_p) * 100, 3)
    log_odds_clv = round(_logit(close_p) - _logit(entry_p), 4)
    return {"decimal_clv_pct": decimal_clv_pct, "implied_prob_clv_pct": implied_prob_clv_pct,
           "log_odds_clv": log_odds_clv}


# ------------------------------------------------------------------ cross-tab

def forward_executability_primary_state_cross_tab(db: Session) -> dict:
    """Task 2: FORWARD_V037B_PLUS rows only. ALWAYS uses a fresh, read-only
    recompute (classify_paper_trade, never the stored ExecutionClassification
    row) -- confirmed live during this release that stored rows can be
    stale (822 of 4600 forward rows carried pre-rename/pre-fix label
    strings that don't match the current vocabulary at all, from the
    still-running main-repo collector re-classifying with old code every
    15 minutes). Reporting off stale stored data would silently reintroduce
    the exact hindsight bug this release fixes. Fails closed
    (FORWARD_REPORTING_UNTRUSTWORTHY) if row/column totals don't reconcile
    exactly against forward_clean_n."""
    stored_rows = db.scalars(select(ExecutionClassification).where(
        ExecutionClassification.is_historical_degraded.is_(False))).all()
    forward_clean_n = len(stored_rows)

    trade_ids = [r.paper_trade_id for r in stored_rows]
    trades_by_id = {t.id: t for t in db.scalars(select(PaperTrade).where(
        PaperTrade.id.in_(trade_ids))).all()}

    seen_trade_ids = set()
    duplicate_count = 0
    table = {ex: {ps: 0 for ps in PRIMARY_STATES} for ex in EXECUTABILITY_CLASSES}
    unrecognized = 0
    stale_vs_stored_diffs = 0
    for stored in stored_rows:
        if stored.paper_trade_id in seen_trade_ids:
            duplicate_count += 1
            continue
        seen_trade_ids.add(stored.paper_trade_id)
        trade = trades_by_id.get(stored.paper_trade_id)
        if trade is None:
            unrecognized += 1
            continue
        primary, _flags, degraded, executability = ecv2.classify_paper_trade(db, trade)
        if executability != stored.executability_label or primary != stored.primary_state:
            stale_vs_stored_diffs += 1
        ex = executability if executability in EXECUTABILITY_CLASSES else None
        ps = primary if primary in PRIMARY_STATES else None
        if ex is None or ps is None:
            unrecognized += 1
            continue
        table[ex][ps] += 1

    row_totals = {ex: sum(table[ex].values()) for ex in EXECUTABILITY_CLASSES}
    col_totals = {ps: sum(table[ex][ps] for ex in EXECUTABILITY_CLASSES) for ps in PRIMARY_STATES}
    accounted = sum(row_totals.values())

    reconciled = (accounted + duplicate_count + unrecognized == forward_clean_n
                 and sum(row_totals.values()) == accounted
                 and sum(col_totals.values()) == accounted
                 and unrecognized == 0)

    return {
        "status": "OK" if reconciled else "FORWARD_REPORTING_UNTRUSTWORTHY",
        "computed_from": "fresh recompute (classify_paper_trade), not stored ExecutionClassification rows",
        "forward_clean_n": forward_clean_n,
        "accounted_n": accounted,
        "duplicate_paper_trade_ids_found": duplicate_count,
        "unrecognized_label_rows": unrecognized,
        "stale_stored_rows_vs_fresh_recompute": stale_vs_stored_diffs,
        "cross_tab": table,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "reconciled": reconciled,
    }


def stale_vs_fresh_classification_check(db: Session, sample_limit: int = 500) -> dict:
    """Compares STORED ExecutionClassification rows against a fresh,
    read-only recompute (classify_paper_trade, never classify_and_store) --
    never mutates. Reports how many stored rows are stale relative to the
    current code (e.g. computed before the v0.3.7D/D.1 reference-timestamp
    fixes)."""
    trades = db.scalars(select(PaperTrade).where(
        PaperTrade.signal_source == "MODEL").limit(sample_limit)).all()
    stored_by_id = {r.paper_trade_id: r for r in db.scalars(select(ExecutionClassification)).all()}
    diffs = 0
    checked = 0
    for t in trades:
        stored = stored_by_id.get(t.id)
        if stored is None:
            continue
        checked += 1
        primary, _flags, degraded, executability = ecv2.classify_paper_trade(db, t)
        if (stored.primary_state != primary or stored.is_historical_degraded != degraded
                or stored.executability_label != executability):
            diffs += 1
    return {"checked": checked, "stale_count": diffs,
           "note": "Read-only comparison -- stored rows were NOT mutated. Run "
                   "execution_classifier_v2.classify_all(db) to refresh stored classifications "
                   "(idempotent, safe to re-run)."}


# ------------------------------------------------------------------ strict CLV

def _strict_forward_pairs(db: Session, lead_s: float) -> dict:
    """Exclusion waterfall + the surviving strict-executable-forward pairs
    for one lead-time gate. INTERSECT: FORWARD_V037B_PLUS x
    EXECUTABLE_PREKICK_STRICT(lead_s) x valid close quality x complete
    3-way market."""
    samples = winner_edge._model_samples(db)
    waterfall = {"all_distinct_match_selection_samples": len(samples)}

    with_close = []
    for s in samples:
        close = db.scalar(select(ClosingRecord).where(
            ClosingRecord.match_id == s["match_id"], ClosingRecord.sportsbook == "bet365",
            ClosingRecord.market == "ML_3WAY", ClosingRecord.selection == s["selection"]))
        if close is not None and close.close_price_decimal is not None:
            with_close.append((s, close))
    waterfall["with_any_closing_record"] = len(with_close)

    forward_era = [(s, c) for s, c in with_close
                  if c.close_polled_at is not None and c.close_ingested_at is not None]
    waterfall["forward_era_v037b_plus"] = len(forward_era)

    strict = []
    for s, c in forward_era:
        pred = db.get(PredictionLedger, s["prediction_id"])
        match = db.get(Match, s["match_id"])
        label = ecv2.compute_executability(db, pred, match, min_lead_s=lead_s)
        if label == ecv2.EXECUTABLE_PREKICK_STRICT:
            strict.append((s, c, pred))
    waterfall["strict_executable_prekick"] = len(strict)

    valid_quality = [(s, c, p) for s, c, p in strict if c.close_quality in VALID_CLOSE_QUALITIES]
    waterfall["valid_close_quality"] = len(valid_quality)

    complete_market = [(s, c, p) for s, c, p in valid_quality if c.all_three_outcomes_present]
    waterfall["complete_three_way_market"] = len(complete_market)

    no_dup = []
    seen_keys = set()
    for s, c, p in complete_market:
        key = (s["match_id"], s["selection"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        no_dup.append((s, c, p))
    waterfall["duplicate_signals_removed"] = len(complete_market) - len(no_dup)
    waterfall["final_strict_decisional_n"] = len(no_dup)

    return {"waterfall": waterfall, "rows": no_dup}


def strict_forward_clv(db: Session, lead_s: float, decision_grade_min_n: int = 150,
                       directional_min_n: int = 50, roi_min_n: int = 300) -> dict:
    """Task 4. The single highlighted metric this whole release exists to
    compute correctly: STRICT EXECUTABLE FORWARD CLV at one lead-time gate."""
    result = _strict_forward_pairs(db, lead_s)
    rows = result["rows"]
    n = len(rows)

    clvs = [_clv_triple(s["current_decimal"], c.close_price_decimal) for s, c, p in rows]
    avg_decimal_clv = round(sum(x["decimal_clv_pct"] for x in clvs) / n, 3) if n else None
    avg_implied_clv = round(sum(x["implied_prob_clv_pct"] for x in clvs) / n, 3) if n else None
    avg_log_odds_clv = round(sum(x["log_odds_clv"] for x in clvs) / n, 4) if n else None

    if n == 0:
        trust_grade = "NOT DECISIONAL -- zero strict decisional samples"
    elif n < directional_min_n:
        trust_grade = f"NOT DECISIONAL -- n={n} below directional threshold ({directional_min_n})"
    elif n < decision_grade_min_n:
        trust_grade = f"DIRECTIONAL -- n={n}, below decision-grade threshold ({decision_grade_min_n})"
    else:
        trust_grade = f"EVIDENCE/DECISION_GRADE eligible on sample size -- n={n}"

    # bootstrap CI on decimal CLV, only when n meets the directional floor
    ci = None
    if n >= directional_min_n:
        import random
        vals = [x["decimal_clv_pct"] for x in clvs]
        boot_means = []
        rng = random.Random(1234)
        for _ in range(1000):
            sample = [vals[rng.randrange(n)] for _ in range(n)]
            boot_means.append(sum(sample) / n)
        boot_means.sort()
        ci = {"lower_95": round(boot_means[int(0.025 * len(boot_means))], 3),
             "upper_95": round(boot_means[int(0.975 * len(boot_means))], 3)}

    return {
        "lead_time_gate_s": lead_s,
        "exclusion_waterfall": result["waterfall"],
        "strict_executable_forward_clv_n": n,
        "avg_decimal_clv_pct": avg_decimal_clv,
        "avg_implied_prob_clv_pct": avg_implied_clv,
        "avg_log_odds_clv": avg_log_odds_clv,
        "bootstrap_95pct_ci_decimal_clv_pct": ci,
        "trust_grade": trust_grade,
        "roi_descriptive_only_note": f"ROI would only be reported once filled-trade n >= {roi_min_n}; "
                                     "this metric is CLV (entry-vs-close), not realized paper ROI.",
    }


def strict_forward_clv_all_gates(db: Session) -> dict:
    return {f"lead_{int(g)}s": strict_forward_clv(db, g) for g in LEAD_TIME_GATES_S}


# ------------------------------------------------------------------ paired baseline

def paired_market_baseline_comparison(db: Session, lead_s: float = 20.0) -> dict:
    """Task 5. Paired CurrentModel vs. FavoriteBaseline comparison on the
    SAME strict executable-forward unique-event universe used by
    strict_forward_clv -- never an unpaired standard-error approximation."""
    result = _strict_forward_pairs(db, lead_s)
    rows = result["rows"]

    both_correct = both_wrong = model_only = baseline_only = 0
    scored_n = 0
    for s, c, p in rows:
        if s["winner_correct"] is None or s["favorite_correct"] is None:
            continue
        scored_n += 1
        if s["winner_correct"] and s["favorite_correct"]:
            both_correct += 1
        elif not s["winner_correct"] and not s["favorite_correct"]:
            both_wrong += 1
        elif s["winner_correct"] and not s["favorite_correct"]:
            model_only += 1
        else:
            baseline_only += 1

    model_wins = both_correct + model_only
    baseline_wins = both_correct + baseline_only
    paired_diff_pct = round(100 * (model_only - baseline_only) / scored_n, 2) if scored_n else None

    # McNemar's test (discordant pairs only), chi-square approx with
    # continuity correction; exact binomial for small discordant counts.
    mcnemar = None
    discordant = model_only + baseline_only
    if discordant > 0:
        if discordant < 25:
            # exact two-sided binomial test on the discordant pairs
            from math import comb
            k = min(model_only, baseline_only)
            p_value = sum(comb(discordant, i) for i in range(0, k + 1)) * 2 / (2 ** discordant)
            p_value = min(p_value, 1.0)
            mcnemar = {"method": "exact_binomial", "discordant_pairs": discordant, "p_value": round(p_value, 4)}
        else:
            chi2 = ((abs(model_only - baseline_only) - 1) ** 2) / discordant
            # chi-square(1) survival function via a standard approximation
            p_value = math.erfc(math.sqrt(chi2 / 2))
            mcnemar = {"method": "mcnemar_chi_square_continuity_corrected",
                      "discordant_pairs": discordant, "chi2": round(chi2, 3), "p_value": round(p_value, 4)}

    return {
        "lead_time_gate_s": lead_s,
        "unique_match_count": len({s["match_id"] for s, c, p in rows}),
        "unique_decision_count": len(rows),
        "duplicate_rows_removed": result["waterfall"]["duplicate_signals_removed"],
        "scored_n": scored_n,
        "model_wins": model_wins, "baseline_wins": baseline_wins,
        "both_correct": both_correct, "both_wrong": both_wrong,
        "model_only_correct": model_only, "baseline_only_correct": baseline_only,
        "paired_difference_pct": paired_diff_pct,
        "mcnemar_test": mcnemar,
        "significant_baseline_outperformance": bool(
            mcnemar and mcnemar.get("p_value") is not None and mcnemar["p_value"] < 0.05
            and baseline_only > model_only),
    }
