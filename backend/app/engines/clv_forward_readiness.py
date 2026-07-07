"""CLV reporting, limited and labeled (v0.3.7B Section 7). Report-only --
no CLV persistence in the DB.

Historical CLV uses provider-time only (OddsSnapshot.collected_at) and is
ALWAYS labeled DEGRADED / non-executable proxy, never used for a kill
decision. Per v0.3.7A Gate G4, the strict-close survivor count is 54 --
directional-grade only (>=50), not decision-grade (needs >=150). This
module reuses that exact threshold logic rather than re-deriving a
different waterfall.

Forward CLV (system-availability-time, from ClosingRecord rows with real
polled_at/ingested_at) is reported separately and will show near-zero
sample size until forward collection has run for a while -- this module
says so plainly rather than padding the number.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import odds_math
from .closing_records import HIGH
from ..models import ClosingRecord, Match, PredictionLedger

DIRECTIONAL_GRADE_MIN_N = 50
DECISION_GRADE_MIN_N = 150

DEGRADED_LABEL = "DEGRADED"
DEGRADED_REASON = ("Historical odds rows do not have true system-availability timestamps "
                   "(v0.3.7A Gate G1) -- collected_at is provider event-time, not our poll/ingest "
                   "time. CLV computed from these rows is a non-executable proxy.")


def _entry_close_pairs(db: Session) -> list[dict]:
    """One row per (match_id, selection) with both a frozen prediction entry
    price and a ClosingRecord. Uses the LATEST prediction_time per group,
    same dedup convention as profit_gates/winner_edge."""
    preds = db.scalars(select(PredictionLedger).where(PredictionLedger.market == "ML_3WAY",
                                                       PredictionLedger.sportsbook == "bet365")).all()
    best: dict[tuple, PredictionLedger] = {}
    for p in preds:
        key = (p.match_id, p.selection)
        if key not in best or p.prediction_time > best[key].prediction_time:
            best[key] = p

    out = []
    for (match_id, selection), p in best.items():
        close = db.scalar(select(ClosingRecord).where(
            ClosingRecord.match_id == match_id, ClosingRecord.sportsbook == "bet365",
            ClosingRecord.market == "ML_3WAY", ClosingRecord.selection == selection))
        if close is None or close.close_price_decimal is None:
            continue
        out.append({
            "match_id": match_id, "selection": selection,
            "entry_decimal": p.current_decimal, "close_decimal": close.close_price_decimal,
            "close_quality": close.close_quality,
            "has_system_ts": close.close_polled_at is not None and close.close_ingested_at is not None,
            "all_three_present": close.all_three_outcomes_present,
        })
    return out


def _devig_close(db: Session, match_id: int, selection: str) -> float | None:
    rows = db.scalars(select(ClosingRecord).where(
        ClosingRecord.match_id == match_id, ClosingRecord.sportsbook == "bet365",
        ClosingRecord.market == "ML_3WAY")).all()
    if len(rows) < 2 or not all(r.close_price_decimal for r in rows):
        return None
    implied = {r.selection: odds_math.implied_prob(r.close_price_decimal) for r in rows}
    fair = dict(zip(implied.keys(), odds_math.remove_vig(list(implied.values()))))
    return fair.get(selection)


def historical_clv_report(db: Session) -> dict:
    pairs = _entry_close_pairs(db)
    n = len(pairs)
    high_quality_pairs = [p for p in pairs if p["close_quality"] == HIGH]

    clvs = []
    for p in pairs:
        try:
            clvs.append(odds_math.clv_pct(p["entry_decimal"], p["close_decimal"]))
        except (ValueError, ZeroDivisionError):
            continue
    avg_clv = round(100 * sum(clvs) / len(clvs), 2) if clvs else None

    exclusion_waterfall = {
        "distinct_match_selection_with_frozen_prediction": len({(p.match_id, p.selection) for p in
            db.scalars(select(PredictionLedger).where(PredictionLedger.market == "ML_3WAY",
                                                       PredictionLedger.sportsbook == "bet365")).all()}),
        "with_any_closing_record": n,
        "with_high_quality_closing_record": len(high_quality_pairs),
        "directional_grade_threshold": DIRECTIONAL_GRADE_MIN_N,
        "decision_grade_threshold": DECISION_GRADE_MIN_N,
    }

    n_high = len(high_quality_pairs)
    if n_high >= DECISION_GRADE_MIN_N:
        grade = "DECISION-GRADE (by sample size only -- still DEGRADED, provider-time)"
    elif n_high >= DIRECTIONAL_GRADE_MIN_N or n >= DIRECTIONAL_GRADE_MIN_N:
        grade = "DIRECTIONAL ONLY -- not decision-grade"
    else:
        grade = "INSUFFICIENT SAMPLE"

    return {
        "status": DEGRADED_LABEL,
        "reason": DEGRADED_REASON,
        "clock_used": "provider-time (OddsSnapshot.collected_at)",
        "distinct_samples_with_close": n,
        "high_quality_close_samples": n_high,
        "avg_provider_time_clv_pct": avg_clv,
        "sample_grade": grade,
        "v0_3_7a_g4_reference_strict_close_n": 54,
        "exclusion_waterfall": exclusion_waterfall,
        "kill_decision_note": "Never used for a kill decision -- see hard rules, this release.",
    }


def forward_clv_readiness(db: Session) -> dict:
    """System-availability-time CLV -- only computable from ClosingRecord
    rows that carry real polled_at/ingested_at (i.e. built from rows
    collected after this release shipped)."""
    pairs = _entry_close_pairs(db)
    forward_pairs = [p for p in pairs if p["has_system_ts"]]
    n = len(forward_pairs)

    clvs = []
    for p in forward_pairs:
        try:
            clvs.append(odds_math.clv_pct(p["entry_decimal"], p["close_decimal"]))
        except (ValueError, ZeroDivisionError):
            continue
    avg_clv = round(100 * sum(clvs) / len(clvs), 2) if clvs else None

    filled_only = [p for p in forward_pairs if p["all_three_present"]]

    if n == 0:
        readiness = "NOT READY -- zero forward (system-timestamped) closing records exist yet"
    elif n < DIRECTIONAL_GRADE_MIN_N:
        readiness = f"NOT READY -- n={n} below directional threshold ({DIRECTIONAL_GRADE_MIN_N})"
    elif n < DECISION_GRADE_MIN_N:
        readiness = f"DIRECTIONAL ONLY -- n={n}, below decision-grade threshold ({DECISION_GRADE_MIN_N})"
    else:
        readiness = f"DECISION-GRADE eligible on sample size -- n={n}"

    return {
        "status": "PENDING FORWARD COLLECTION" if n == 0 else "PARTIAL",
        "clock_used": "system-availability-time (ingested_at/polled_at)",
        "forward_system_timestamped_samples": n,
        "all_signal_avg_clv_pct": avg_clv,
        "filled_only_count": len(filled_only),
        "readiness_verdict": readiness,
        "note": ("This will read near-zero immediately after this release ships -- it requires "
                "the poller to run and accumulate new OddsSnapshot/ClosingRecord rows with real "
                "polled_at/ingested_at before it can say anything. That is expected, not a bug."),
    }
