"""Profit Kill Gates + Profit Readiness Dashboard (v0.3.6 Modules 7-8).

Every gate defaults to NOT ENOUGH DATA below its minimum sample size --
never PASS by default, never silently softened. "Ready for live small
stakes" is the AND of every gate and is FAIL/NOT ENOUGH DATA unless every
single gate PASSes.

Do NOT assume BetsAPI/bet365 has a <=15s live-reaction capability. See
engines/execution_strategy.py -- the observed floor is ~20-30s, and the
feed gate is built to reflect that honestly.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (BookmakerCoverage, FriendPick, FriendPickScore, Match,
                      OddsSnapshot, PaperTrade, PredictionLedger,
                      PredictionReality, PredictionScore)
from .friend_picks import favorite_selection

MIN_LATENCY_SAMPLE = 30
MIN_SIGNAL_SAMPLE = 30
MIN_EXECUTION_SAMPLE = 30
PRE_KICKOFF_MIN_SAMPLE = 10
PRE_KICKOFF_FRESHNESS_S = 60
PRE_KICKOFF_PASS_PCT = 80.0
LIVE_OPEN_STRESS_S = 45.0
SIGNAL_GATE_MARGIN_PTS = 5.0
EXECUTION_GATE_MIN_SURVIVAL_PCT = 60.0
RISK_GATE_MAX_DRAWDOWN_UNITS = 15.0
WINNER_EDGE_MIN_MODEL_SAMPLE = 50
WINNER_EDGE_MIN_FRIEND_SAMPLE = 30
# v0.3.6.2 Part C3: a pick is "likely test data" for gating purposes if
# logged more than this long after its own kickoff -- same heuristic used
# for FriendPick.likely_test_artifact in engines/friend_picks.py.
FRIEND_TEST_ARTIFACT_THRESHOLD_S = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _p95(values: list[float]) -> float:
    if len(values) >= 2:
        return round(statistics.quantiles(sorted(values), n=20)[18], 2)
    return values[0]


def pipeline_health(db: Session) -> dict:
    n_matches = db.scalar(select(func.count(Match.id))) or 0
    n_odds = db.scalar(select(func.count(OddsSnapshot.id))) or 0
    n_frozen = db.scalar(select(func.count(PredictionLedger.id))) or 0
    n_scored = db.scalar(select(func.count(PredictionScore.id))) or 0
    return {
        "matches_collecting": {"status": "PASS" if n_matches > 0 else "NOT ENOUGH DATA", "n": n_matches},
        "odds_collecting": {"status": "PASS" if n_odds > 0 else "NOT ENOUGH DATA", "n": n_odds},
        "predictions_freezing": {"status": "PASS" if n_frozen > 0 else "NOT ENOUGH DATA", "n": n_frozen},
        "results_scoring": {"status": "PASS" if n_scored > 0 else "NOT ENOUGH DATA", "n": n_scored},
    }


def feed_gate(db: Session) -> dict:
    matches = db.scalars(select(Match).where(Match.ext_id.is_not(None))).all()
    pre_checked = pre_ok = 0
    for m in matches:
        last_pre = db.scalars(select(OddsSnapshot).where(
            OddsSnapshot.match_id == m.id, OddsSnapshot.phase == "pre_match",
        ).order_by(OddsSnapshot.collected_at.desc())).first()
        if last_pre is None:
            continue
        pre_checked += 1
        gap = (m.start_time - last_pre.collected_at).total_seconds()
        if 0 <= gap <= PRE_KICKOFF_FRESHNESS_S:
            pre_ok += 1
    if pre_checked < PRE_KICKOFF_MIN_SAMPLE:
        pre_kickoff = {"status": "NOT ENOUGH DATA", "pct_fresh_pre_kick": None, "n": pre_checked}
    else:
        pct = round(100 * pre_ok / pre_checked, 1)
        pre_kickoff = {"status": "PASS" if pct >= PRE_KICKOFF_PASS_PCT else "FAIL",
                      "pct_fresh_pre_kick": pct, "n": pre_checked}

    latencies = [v for (v,) in db.execute(select(PredictionReality.first_live_after_s)
                                          .where(PredictionReality.first_live_after_s.is_not(None))).all()]
    n = len(latencies)
    if n < MIN_LATENCY_SAMPLE:
        live_open_manual = {"status": "NOT ENOUGH DATA", "n": n, "median_latency_s": None,
                            "p95_latency_s": None, "median_based_status": "NOT ENOUGH DATA",
                            "p95_based_status": "NOT ENOUGH DATA"}
    else:
        median = round(statistics.median(latencies), 2)
        p95 = _p95(latencies)
        median_status = "PASS" if median <= LIVE_OPEN_STRESS_S else "FAIL"
        p95_status = "PASS" if p95 <= LIVE_OPEN_STRESS_S else "FAIL"
        live_open_manual = {"status": "PASS" if (median_status == "PASS" and p95_status == "PASS") else "FAIL",
                            "n": n, "median_latency_s": median, "p95_latency_s": p95,
                            "median_based_status": median_status, "p95_based_status": p95_status}

    return {
        "pre_kickoff": pre_kickoff,
        "live_open_manual": live_open_manual,
        "note": ("BetsAPI/bet365 fails the original <=15s live-reaction assumption -- "
                "observed 0-5%% within 15s across two clean validation sessions. It may "
                "still pass PRE_KICKOFF (pre-kick snapshot freshness) or the 30-45s "
                "LIVE_OPEN_MANUAL stress test above."),
    }


def _steam_sample(db: Session, source: str) -> list[tuple[bool, int, str, datetime]]:
    """Returns RAW [(subject_steam_correct, match_id, selection, sample_time), ...]
    rows, restricted to gold/silver reality tier where that's determinable.
    Deliberately NOT deduplicated here -- one match/selection frozen at
    multiple horizons produces multiple PredictionScore rows that all
    describe the SAME underlying reality outcome (did this selection's
    price shorten), not independent trials. signal_gate() dedupes so both
    raw_rows and distinct_samples can be reported honestly (v0.3.6.1 audit
    fix -- v0.3.6 reported n=446 raw rows as if independent, when only 58
    distinct matches / 174 distinct (match,selection) pairs backed it)."""
    out = []
    if source == "model":
        rows = db.scalars(select(PredictionScore).where(
            PredictionScore.steam_direction_correct.is_not(None))).all()
        for r in rows:
            pred = db.get(PredictionLedger, r.prediction_id)
            reality = db.get(PredictionReality, r.reality_id)
            if pred and reality and reality.dataset_tier in ("gold", "silver"):
                out.append((r.steam_direction_correct, pred.match_id, pred.selection, pred.prediction_time))
    else:
        rows = db.scalars(select(FriendPickScore).where(
            FriendPickScore.steam_direction_correct.is_not(None))).all()
        for r in rows:
            pick = db.get(FriendPick, r.friend_pick_id)
            if pick and pick.match_id:
                out.append((r.steam_direction_correct, pick.match_id, pick.pick_side, pick.effective_known_at))
    return out


def _dedup_by_match_selection(sample: list[tuple[bool, int, str, datetime]]) -> dict[tuple[int, str], bool]:
    """One value per (match_id, selection) -- repeated horizons of the same
    match/selection are the same underlying reality outcome, not independent
    samples. Keeps the row with the latest sample_time per group (closest to
    kickoff / most information), so the result is deterministic regardless
    of DB row-return order."""
    best: dict[tuple[int, str], tuple[datetime, bool]] = {}
    for correct, match_id, selection, sample_time in sample:
        key = (match_id, selection)
        if key not in best or sample_time > best[key][0]:
            best[key] = (sample_time, correct)
    return {k: v[1] for k, v in best.items()}


def signal_gate(db: Session, source: str) -> dict:
    sample = _steam_sample(db, source)
    raw_rows = len(sample)
    dedup = _dedup_by_match_selection(sample)
    distinct_samples = len(dedup)
    # Gate threshold and accuracy use distinct_samples, NEVER raw_rows --
    # raw_rows is reported alongside purely for transparency (v0.3.6.1 fix).
    n = distinct_samples
    if n < MIN_SIGNAL_SAMPLE:
        return {"status": "NOT ENOUGH DATA", "n": n, "raw_rows": raw_rows,
               "distinct_samples": distinct_samples, "accuracy_pct": None,
               "baseline_accuracy_pct": None, "margin_pts": None}
    correct_count = sum(1 for c in dedup.values() if c)
    accuracy = 100 * correct_count / n

    baseline_correct = 0
    baseline_n = 0
    for (match_id, _selection) in dedup:
        m = db.get(Match, match_id)
        if not m:
            continue
        reality_any = db.scalar(select(PredictionReality).where(PredictionReality.match_id == match_id))
        if not reality_any:
            continue
        fav = favorite_selection(db, match_id, reality_any.sportsbook, "ML_3WAY", m.start_time)
        fav_reality = db.scalar(select(PredictionReality).where(
            PredictionReality.match_id == match_id, PredictionReality.selection == fav,
            PredictionReality.market == "ML_3WAY")) if fav else None
        if fav_reality is None or fav_reality.actual_shortened is None:
            continue
        baseline_n += 1
        if fav_reality.actual_shortened:  # baseline claims favorite shortens
            baseline_correct += 1
    baseline_accuracy = (100 * baseline_correct / baseline_n) if baseline_n else None
    margin = (accuracy - baseline_accuracy) if baseline_accuracy is not None else None
    status = "PASS" if (margin is not None and margin >= SIGNAL_GATE_MARGIN_PTS) else "FAIL"
    return {"status": status, "n": n, "raw_rows": raw_rows, "distinct_samples": distinct_samples,
           "accuracy_pct": round(accuracy, 1),
           "baseline_accuracy_pct": round(baseline_accuracy, 1) if baseline_accuracy is not None else None,
           "margin_pts": round(margin, 1) if margin is not None else None,
           "baseline_n": baseline_n}


def execution_gate(db: Session) -> dict:
    rows = db.scalars(select(PaperTrade).where(PaperTrade.delay_seconds == 30)).all()
    n = len(rows)
    model_n = sum(1 for r in rows if r.signal_source == "MODEL")
    friend_n = sum(1 for r in rows if r.signal_source == "FRIEND")
    if n < MIN_EXECUTION_SAMPLE:
        return {"status": "NOT ENOUGH DATA", "n": n, "model_n": model_n, "friend_n": friend_n,
               "survival_pct": None}
    survived = sum(1 for r in rows if r.entry_survived)
    pct = round(100 * survived / n, 1)
    return {"status": "PASS" if pct >= EXECUTION_GATE_MIN_SURVIVAL_PCT else "FAIL",
           "n": n, "model_n": model_n, "friend_n": friend_n, "survival_pct": pct}


def book_gate(db: Session) -> dict:
    candidates = db.scalars(select(BookmakerCoverage).where(
        BookmakerCoverage.execution_candidate.is_(True), BookmakerCoverage.status == "WORKS",
        BookmakerCoverage.ml_3way_available.is_(True),
    )).all()
    return {"status": "PASS" if candidates else "FAIL",
           "verified_books": [c.source_name for c in candidates]}


def risk_gate(db: Session) -> dict:
    settled = db.scalars(select(PaperTrade).where(
        PaperTrade.settlement_status == "SETTLED", PaperTrade.paper_pl_usd.is_not(None),
    ).order_by(PaperTrade.created_at)).all()
    n = len(settled)
    model_n = sum(1 for r in settled if r.signal_source == "MODEL")
    friend_n = sum(1 for r in settled if r.signal_source == "FRIEND")
    if n < MIN_EXECUTION_SAMPLE:
        return {"status": "NOT ENOUGH DATA", "n": n, "model_n": model_n, "friend_n": friend_n,
               "max_drawdown_units": None}
    cum = 0.0
    peak = 0.0
    max_dd_usd = 0.0
    for r in settled:
        cum += r.paper_pl_usd
        peak = max(peak, cum)
        max_dd_usd = max(max_dd_usd, peak - cum)
    # Convert drawdown (USD) to "units" using the configured paper stake unit.
    from ..models import Settings
    s = db.get(Settings, 1)
    unit_usd = (s.paper_stake_usd if s else 100.0) or 100.0
    max_dd_units = round(max_dd_usd / unit_usd, 2)
    return {"status": "PASS" if max_dd_units <= RISK_GATE_MAX_DRAWDOWN_UNITS else "FAIL",
           "n": n, "model_n": model_n, "friend_n": friend_n,
           "max_drawdown_units": max_dd_units, "max_drawdown_usd": round(max_dd_usd, 2)}


def _friend_clean_scored_sample(db: Session) -> tuple[int, float | None]:
    """n and winner accuracy over scored friend picks that are NEITHER
    backfilled NOR a likely test artifact -- the winner_edge_gate's own,
    stricter definition of "clean", separate from signal_gate's broader
    steam-direction sample."""
    picks = db.scalars(select(FriendPick).where(FriendPick.is_backfilled.is_(False))).all()
    scores = {s.friend_pick_id: s for s in db.scalars(select(FriendPickScore)).all()}
    clean = []
    for p in picks:
        if p.id not in scores:
            continue
        if p.kickoff_time and (p.created_at - p.kickoff_time).total_seconds() > FRIEND_TEST_ARTIFACT_THRESHOLD_S:
            continue
        clean.append(scores[p.id])
    n = len(clean)
    w = [s.winner_correct for s in clean if s.winner_correct is not None]
    acc = round(100 * sum(w) / len(w), 1) if w else None
    return n, acc


def winner_edge_gate(db: Session, source: str) -> dict:
    """PASS requires: enough independent samples, winner accuracy beating
    the favorite baseline, AND at least one delay bucket with non-negative
    paper ROI. Missing paper-trade data always forces NOT ENOUGH DATA, never
    a default PASS -- ROI is null everywhere until POST
    /api/paper-trades/simulate-all has actually run."""
    from . import winner_edge as we
    if source == "model":
        rep = we.model_report(db)
        n = rep["distinct_samples"]
        winner_acc = rep["winner_accuracy_pct"]
        fav_acc = rep["favorite_baseline_accuracy_pct"]
        min_n = WINNER_EDGE_MIN_MODEL_SAMPLE
        roi_by_delay = rep["roi_pct_by_delay_seconds"]
    else:
        rep = we.friend_report(db)
        n, winner_acc = _friend_clean_scored_sample(db)
        fav_acc = rep["favorite_baseline_accuracy_pct"]
        min_n = WINNER_EDGE_MIN_FRIEND_SAMPLE
        roi_by_delay = rep["roi_pct_by_delay_seconds"]

    margin = round(winner_acc - fav_acc, 1) if (winner_acc is not None and fav_acc is not None) else None
    has_paper_trade_data = any(v is not None for v in roi_by_delay.values())

    if n < min_n or not has_paper_trade_data:
        return {"status": "NOT ENOUGH DATA", "n": n, "winner_accuracy_pct": winner_acc,
               "favorite_baseline_pct": fav_acc, "margin_pts": margin, "roi_by_delay": roi_by_delay}

    beats_baseline = margin is not None and margin > 0
    any_non_negative_roi = any(v is not None and v >= 0 for v in roi_by_delay.values())
    status = "PASS" if (beats_baseline and any_non_negative_roi) else "FAIL"
    return {"status": status, "n": n, "winner_accuracy_pct": winner_acc,
           "favorite_baseline_pct": fav_acc, "margin_pts": margin, "roi_by_delay": roi_by_delay}


def compute_all_gates(db: Session) -> dict:
    feed = feed_gate(db)
    signal_model = signal_gate(db, "model")
    signal_friend = signal_gate(db, "friend")
    execution = execution_gate(db)
    book = book_gate(db)
    risk = risk_gate(db)
    winner_edge_model = winner_edge_gate(db, "model")
    winner_edge_friend = winner_edge_gate(db, "friend")
    health = pipeline_health(db)

    gates = {
        "feed_gate": feed, "signal_gate_model": signal_model, "signal_gate_friend": signal_friend,
        "execution_gate": execution, "book_gate": book, "risk_gate": risk,
        "winner_edge_gate_model": winner_edge_model, "winner_edge_gate_friend": winner_edge_friend,
    }
    statuses = [feed["pre_kickoff"]["status"], feed["live_open_manual"]["status"],
               signal_model["status"], signal_friend["status"], execution["status"],
               book["status"], risk["status"], winner_edge_model["status"], winner_edge_friend["status"]]
    if any(s == "NOT ENOUGH DATA" for s in statuses):
        overall = "NOT ENOUGH DATA"
    elif all(s == "PASS" for s in statuses):
        overall = "PASS"
    else:
        overall = "FAIL"

    viable_pre_kickoff = feed["pre_kickoff"]["status"]
    viable_30_45_delay = feed["live_open_manual"]["status"]

    return {
        "pipeline_health": health,
        "gates": gates,
        "viable_pre_kickoff": viable_pre_kickoff,
        "viable_at_30_45s_delay": viable_30_45_delay,
        "ready_for_live_small_stakes": overall,
        "disclaimer": ("Default is NOT ENOUGH DATA or FAIL until every gate genuinely passes. "
                       "BetsAPI/bet365's <=15s live-reaction assumption is not assumed passed -- "
                       "see feed_gate.live_open_manual."),
    }
