"""Entry floor diagnostics (v0.3.7B Section 5). Analysis only -- does NOT
change betting/entry logic.

Per v0.3.7A Gate G3: maximum_entry_decimal is a misleading legacy name for a
MINIMUM acceptable entry floor. FILLED = price >= floor is correct for a
back bet. The unresolved finding this module formally confirms/extends: the
floor equaled the exact signal-time price in 100% of 653 historical
predictions because steam_probability never reached the 0.55 trigger in
steam.py. This module measures whether that's still true and simulates
(report-only) what a lower floor would hypothetically have changed --
it does not implement any change.
"""
from __future__ import annotations

import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import odds_math
from .paper_trade import DELAYS_SECONDS
from ..models import Match, OddsSnapshot, PredictionLedger

WHATIF_DISCOUNT_PCTS = (0.01, 0.02, 0.04, 0.06)
STEAM_TRIGGER_THRESHOLD = 0.55


def _price_at_or_after(db: Session, match_id: int, sportsbook: str, market: str,
                       selection: str, at) -> float | None:
    """Best-effort nearby price lookup for what-if simulation, reusing the
    same at-or-before-target convention as execution_pricing.price_at_delay
    but simplified (no staleness cutoff) since this is analysis-only."""
    row = db.scalar(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.selection == selection,
        OddsSnapshot.collected_at <= at,
    ).order_by(OddsSnapshot.collected_at.desc()))
    return row.decimal_odds if row else None


def run(db: Session) -> dict:
    preds = db.scalars(select(PredictionLedger)).all()
    total = len(preds)

    floor_eq_signal = 0
    floor_lt_signal = 0
    floor_gt_signal = 0
    steam_probs = []
    for p in preds:
        if p.steam_probability is not None:
            steam_probs.append(p.steam_probability)
        if p.maximum_entry_decimal is not None and p.current_decimal is not None:
            if p.maximum_entry_decimal == p.current_decimal:
                floor_eq_signal += 1
            elif p.maximum_entry_decimal < p.current_decimal:
                floor_lt_signal += 1
            else:
                floor_gt_signal += 1

    steam_dist = None
    if steam_probs:
        steam_dist = {
            "n": len(steam_probs), "min": round(min(steam_probs), 3),
            "max": round(max(steam_probs), 3), "mean": round(statistics.mean(steam_probs), 3),
            "median": round(statistics.median(steam_probs), 3),
            "count_gte_055_trigger": sum(1 for s in steam_probs if s >= STEAM_TRIGGER_THRESHOLD),
            "pct_gte_055_trigger": round(100 * sum(1 for s in steam_probs if s >= STEAM_TRIGGER_THRESHOLD)
                                        / len(steam_probs), 2),
        }

    # What-if: for predictions whose floor==current_decimal (the anomaly),
    # simulate a floor discounted by 1/2/4/6% below the signal price, and
    # check (report-only, using REAL stored snapshots, never fabricated)
    # whether a nearby snapshot exists whose price would have cleared that
    # lower floor -- i.e. would this specific historical MISSED_PRICE-due-
    # to-floor row have instead been fillable.
    whatif = {}
    affected = [p for p in preds if p.maximum_entry_decimal is not None and p.current_decimal is not None
               and p.maximum_entry_decimal == p.current_decimal]
    for pct in WHATIF_DISCOUNT_PCTS:
        would_fill = 0
        checked = 0
        for p in affected:
            hypothetical_floor = p.current_decimal * (1 - pct)
            for delay in DELAYS_SECONDS:
                target = p.prediction_time
                price = _price_at_or_after(db, p.match_id, p.sportsbook, p.market, p.selection, target)
                if price is None:
                    continue
                checked += 1
                if price >= hypothetical_floor:
                    would_fill += 1
        whatif[f"discount_{int(pct*100)}pct"] = {
            "hypothetical_floor_formula": f"current_decimal * (1 - {pct})",
            "rows_checked": checked,
            "hypothetical_fills": would_fill,
            "hypothetical_fill_rate_pct": round(100 * would_fill / checked, 1) if checked else None,
            "clv_roi_note": ("Not computed: requires trustworthy system-availability timestamps "
                             "on the priced snapshot, which historical rows do not have (v0.3.7A "
                             "Gate G1). Would only be computed here once forward-collected, "
                             "timestamped snapshots exist."),
        }

    return {
        "analysis_only_disclaimer": ("ANALYSIS ONLY. This report does not change entry/betting "
                                     "logic. It measures whether the non-functional entry floor "
                                     "(v0.3.7A Gate G3) is a config/threshold issue, and simulates "
                                     "hypothetical outcomes against real historical data -- it does "
                                     "not implement or recommend any change."),
        "total_predictions": total,
        "floor_equals_signal_price_count": floor_eq_signal,
        "floor_below_signal_price_count": floor_lt_signal,
        "floor_above_signal_price_count": floor_gt_signal,
        "floor_equals_signal_price_pct": round(100 * floor_eq_signal / total, 1) if total else None,
        "steam_probability_distribution": steam_dist,
        "steam_trigger_threshold": STEAM_TRIGGER_THRESHOLD,
        "whatif_lower_floor_simulation": whatif,
    }
