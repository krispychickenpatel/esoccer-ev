"""Execution Strategy Shift (v0.3.6 Module 5).

BetsAPI/bet365 ML_3WAY first-live capture does not hit a <=15s reaction
time -- two clean validation sessions (n=30 total) measured avg 26-29s,
median 20-25s, p95 64-84s, ~0-5% within 15s (see
notes/fixed-max2-first-live-validation-report.md and
notes/v0.3.5-provider-execution-fix-report.md). That gap is provider-side
(BetsAPI's own odds feed publish lag), not a poller defect, and no amount of
poller tuning closes it. Every execution-timing decision in this codebase
must assume a 30-45s observed feed lag, not <=15s, until a faster feed is
proven (see engines FeedCandidate / v0.3.6 Module 4).

Decision table (exact, do not soften):

    PRE_KICKOFF      -- edge exists at the current pre-kickoff price
                        (EV >= min_ev_pct) AND steam says the price will
                        shorten (waiting to bet costs money -- bet now).
    LIVE_OPEN_MANUAL -- edge exists only at the predicted first-live price,
                        AND that edge is modeled to survive the 30-45s
                        stress delay (predicted first-live price still
                        clears max_entry_decimal).
    SLOWER_BOOK      -- edge exists AND at least one non-reference book is
                        a verified execution_candidate (BookmakerCoverage)
                        for this league + market. Unavailable otherwise.
    PASS             -- anything else, including "edge exists but dies
                        inside the lag window" (LATENCY_KILLS_EDGE).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BookmakerCoverage
from .odds_math import expected_value

# Stress-test assumption. See module docstring -- do not use 15 here.
OBSERVED_FEED_LAG_LOW_S = 30
OBSERVED_FEED_LAG_HIGH_S = 45

STEAM_SHORTEN_THRESHOLD = 0.55


def classify_execution_mode(
    db: Session,
    *,
    current_decimal: float,
    predicted_first_live_decimal: float | None,
    max_entry_decimal: float | None,
    model_prob: float,
    min_ev_pct: float,
    steam_probability: float,
    market: str = "ML_3WAY",
) -> dict:
    """Returns {"execution_mode": str, "reason_codes": list[str]}."""
    ev_now_pct = expected_value(model_prob, current_decimal) * 100
    reasons: list[str] = []

    if ev_now_pct >= min_ev_pct and steam_probability >= STEAM_SHORTEN_THRESHOLD:
        return {"execution_mode": "PRE_KICKOFF",
                "reason_codes": ["EDGE_NOW", "STEAM_SHORTENING_WAIT_COSTS_MONEY"]}

    if predicted_first_live_decimal is not None:
        ev_live_pct = expected_value(model_prob, predicted_first_live_decimal) * 100
        if ev_live_pct >= min_ev_pct:
            survives_stress = (max_entry_decimal is None
                               or predicted_first_live_decimal >= max_entry_decimal)
            if survives_stress:
                return {"execution_mode": "LIVE_OPEN_MANUAL",
                        "reason_codes": ["EDGE_AT_PREDICTED_FIRST_LIVE",
                                        f"MODELED_TO_SURVIVE_{OBSERVED_FEED_LAG_LOW_S}_{OBSERVED_FEED_LAG_HIGH_S}S_LAG"]}
            reasons.append("LATENCY_KILLS_EDGE")

    candidate = db.scalar(select(BookmakerCoverage).where(
        BookmakerCoverage.execution_candidate.is_(True),
        BookmakerCoverage.status == "WORKS",
        (BookmakerCoverage.ml_3way_available.is_(True) if market == "ML_3WAY"
         else BookmakerCoverage.spread_2way_available.is_(True)),
    ))
    if candidate is not None and (ev_now_pct >= min_ev_pct
                                  or (predicted_first_live_decimal is not None
                                      and expected_value(model_prob, predicted_first_live_decimal) * 100 >= min_ev_pct)):
        return {"execution_mode": "SLOWER_BOOK",
                "reason_codes": ["EDGE_EXISTS", f"VERIFIED_BOOK_{candidate.source_name.upper()}"]}

    if not reasons:
        reasons.append("NO_EDGE")
    return {"execution_mode": "PASS", "reason_codes": reasons}
