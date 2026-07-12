"""v0.3.7D.1 Task 11: spot-check readiness -- a coverage report, NOT a
blocker. Paper-simulated FILLED trades are never proof a real book would
actually let you place that trade; spot-checks (manual book-vs-provider
price/availability comparisons, see scripts/spot_check_capture.py) are the
only evidence of real placeability. Zero spot-checks means "not yet
validated", never "execution failed" -- this module must never be read as
a pass/fail gate on the strict CLV verdict.
"""
from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PaperTrade

SPOT_CHECK_CSV = Path("/Users/krispatell/Downloads/ESoccer/notes/triage/book_spot_checks.csv")

# Coverage confidence threshold -- a manual-effort coverage floor, distinct
# from the statistical directional/decision-grade sample gates used
# elsewhere in this release (those measure whether a CLV estimate is
# reliable; this measures whether enough real books have been manually
# checked against the provider feed to trust that simulated fills are
# actually placeable).
SUFFICIENT_SPOT_CHECK_N = 20
PRICE_MATCH_TOLERANCE_PCT = 2.0

SIMULATED_FILLED_LABEL_INSUFFICIENT = "SIMULATED_FILLED -- PLACEABILITY NOT YET VALIDATED"
SIMULATED_FILLED_LABEL_SUFFICIENT = "SIMULATED_FILLED -- PLACEABILITY SPOT-CHECKED"


def _read_spot_checks() -> list[dict]:
    if not SPOT_CHECK_CSV.exists():
        return []
    with open(SPOT_CHECK_CSV) as f:
        return list(csv.DictReader(f))


def _price_within_tolerance(row: dict) -> bool | None:
    try:
        displayed = float(row.get("displayed_price") or "")
        provider = float(row.get("provider_latest_price") or "")
    except (ValueError, TypeError):
        return None
    if provider == 0:
        return None
    pct_diff = abs(displayed - provider) / provider * 100
    return pct_diff <= PRICE_MATCH_TOLERANCE_PCT


def spot_check_readiness_report(db: Session) -> dict:
    simulated_filled_count = len(db.scalars(select(PaperTrade).where(
        PaperTrade.signal_source == "MODEL",
        PaperTrade.settlement_status.in_(("FILLED", "SETTLED")))).all())

    rows = _read_spot_checks()
    spot_check_count = len(rows)
    books_checked = sorted({r.get("book") for r in rows if r.get("book")})

    tolerance_results = [_price_within_tolerance(r) for r in rows]
    tolerance_evaluable = [t for t in tolerance_results if t is not None]
    within_tolerance_n = sum(1 for t in tolerance_evaluable if t)

    availability_pairs = [(r.get("market_available_on_book"), r.get("market_available_on_provider"))
                          for r in rows if r.get("market_available_on_book") and r.get("market_available_on_provider")]
    availability_match_n = sum(1 for book_a, prov_a in availability_pairs if book_a.strip().lower() == prov_a.strip().lower())
    availability_mismatch_n = len(availability_pairs) - availability_match_n

    coverage_pct = (round(100 * spot_check_count / simulated_filled_count, 1)
                   if simulated_filled_count else None)
    sufficient = spot_check_count >= SUFFICIENT_SPOT_CHECK_N
    label = SIMULATED_FILLED_LABEL_SUFFICIENT if sufficient else SIMULATED_FILLED_LABEL_INSUFFICIENT

    return {
        "label": label,
        "simulated_filled_count": simulated_filled_count,
        "spot_check_count": spot_check_count,
        "placeability_validation_coverage_pct": coverage_pct,
        "sufficient_for_validated_label": sufficient,
        "sufficient_threshold_n": SUFFICIENT_SPOT_CHECK_N,
        "books_checked": books_checked,
        "books_checked_count": len(books_checked),
        "price_match_tolerance_pct": PRICE_MATCH_TOLERANCE_PCT,
        "price_within_tolerance_n": within_tolerance_n,
        "price_tolerance_evaluable_n": len(tolerance_evaluable),
        "market_availability_match_n": availability_match_n,
        "market_availability_mismatch_n": availability_mismatch_n,
        "note": ("Zero (or few) spot-checks means placeability is NOT YET VALIDATED, not that "
                "execution failed -- this section is coverage evidence, never a pass/fail gate. "
                "No live betting or automated book interaction is performed anywhere in this pipeline."),
    }
