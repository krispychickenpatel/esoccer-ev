"""Execution classification v2 (v0.3.7B Section 4, v0.3.7D timing fix).

Replaces the old single "missed price" idea with one primary execution
state plus multiple coexisting diagnostic flags, computed per PaperTrade
row. Never mutates PaperTrade.settlement_status -- classifications are
stored in the separate, additive ExecutionClassification table.

Per v0.3.7A: 96.2% of historical MISSED_PRICE rows are NO_DATA, not a floor
breach -- NO_DATA_AT_ENTRY must be the dominant state reported, not
PRICE_BELOW_ENTRY_FLOOR. maximum_entry_decimal is a misleading legacy name
for a MINIMUM acceptable entry floor; this module never uses the phrase
"moved past max entry" -- see PRICE_BELOW_ENTRY_FLOOR.

v0.3.7D fix (notes/triage/v0_3_7D-signal-timing-audit.md): SIGNAL_TOO_LATE
used to compare prediction_time against Match.start_time (the nominal/
scheduled kickoff). Real data showed 29 of 32 audited matches had their
actual observed live-phase start 7-60s (mean 29s) AFTER Match.start_time --
so a prediction frozen shortly after the SCHEDULED time but still before
the TRUE kickoff was being mislabeled as too late. Fixed to compare against
actual/live start (earliest phase='live' OddsSnapshot) when available,
falling back to scheduled start only when no live data exists yet.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (ExecutionClassification, Match, MarketAvailabilityRecord,
                      OddsSnapshot, PaperTrade, PredictionLedger)
from .closing_records import _actual_start
from .market_availability import ABSENT, BOOK_MISSING_MARKET, EMPTY_PROVIDER_RESPONSE, \
    detect_withdrawal_relist_candidates

FILLED = "FILLED"
NO_DATA_AT_ENTRY = "NO_DATA_AT_ENTRY"
PRICE_BELOW_ENTRY_FLOOR = "PRICE_BELOW_ENTRY_FLOOR"
MARKET_UNAVAILABLE_AT_ENTRY = "MARKET_UNAVAILABLE_AT_ENTRY"
BOOK_MISSING_MARKET_STATE = "BOOK_MISSING_MARKET"
SIGNAL_TOO_LATE = "SIGNAL_TOO_LATE"
TIMESTAMP_UNTRUSTWORTHY = "TIMESTAMP_UNTRUSTWORTHY"
UNKNOWN = "UNKNOWN"

# v0.3.7D executable-signal gate (Section 3), v0.3.7D.1 hindsight fix
# (Section 3) -- orthogonal to primary_state.
EXECUTABLE_PREKICK_STRICT = "EXECUTABLE_PREKICK_STRICT"
EXECUTABLE_VIA_START_DELAY = "EXECUTABLE_VIA_START_DELAY"
RESEARCH_ONLY_KICKOFF = "RESEARCH_ONLY_KICKOFF"
LATE_SIGNAL = "LATE_SIGNAL"
UNKNOWN_START_TIME = "UNKNOWN_START_TIME"
MINIMUM_USEFUL_LEAD_SECONDS = 20.0
LEAD_TIME_GATES_S = (20.0, 30.0, 45.0)

STALE_THRESHOLD_S = 60.0
HISTORICAL_CREATED_AT_GAP_S = 300.0  # 5 min: created_at far from signal_time -> backfill artifact


def compute_executability(db: Session, pred: PredictionLedger | None, match: Match | None,
                         min_lead_s: float = MINIMUM_USEFUL_LEAD_SECONDS) -> str:
    """Strict, no-hindsight executability gate (v0.3.7D.1 fix --
    notes/triage/v0_3_7D1-self-challenge.md Q6).

    EXECUTABLE_PREKICK_STRICT requires the lead to be measured against
    SCHEDULED start (Match.start_time) -- the only timestamp knowable AT
    SIGNAL TIME. The v0.3.7D version of this function measured lead against
    actual/live start instead, which is only knowable AFTER the match has
    begun -- using it to decide whether a signal "was executable" is itself
    a hindsight construction (real data: 29 of 32 audited matches had
    actual start 7-60s after scheduled start, so a signal that only clears
    the lead gate because kickoff happened to run late was being counted
    as if a real-time trader could have known that in advance).

    A signal that fails the strict scheduled-start gate but WOULD pass if
    measured against the (retrospectively-known) actual start is
    EXECUTABLE_VIA_START_DELAY -- diagnostic only. Hard rule, this release:
    never use actual start delay to retroactively launder a KICKOFF signal
    into normal pre-kick executability.

    Orthogonal to primary_state: a row can be e.g. NO_DATA_AT_ENTRY AND
    EXECUTABLE_PREKICK_STRICT at the same time (a genuinely timely signal
    that simply had no odds row at the target delay)."""
    if pred is None or match is None or match.start_time is None:
        return UNKNOWN_START_TIME

    if pred.prediction_time <= match.start_time - timedelta(seconds=min_lead_s):
        return EXECUTABLE_PREKICK_STRICT

    actual_start, _used_fallback = _actual_start(db, match)
    if (actual_start is not None and actual_start > match.start_time
            and pred.prediction_time <= actual_start - timedelta(seconds=min_lead_s)):
        return EXECUTABLE_VIA_START_DELAY

    if pred.horizon_label == "KICKOFF":
        return RESEARCH_ONLY_KICKOFF
    return LATE_SIGNAL


def _nearby_availability_state(db: Session, match_id: int, sportsbook: str, market: str,
                               selection: str, at, window_s: float = 120.0) -> str | None:
    rows = db.scalars(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match_id, MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market, MarketAvailabilityRecord.selection == selection,
        MarketAvailabilityRecord.observed_at >= at - timedelta(seconds=window_s),
        MarketAvailabilityRecord.observed_at <= at + timedelta(seconds=window_s),
    ).order_by(MarketAvailabilityRecord.observed_at.desc())).all()
    return rows[0].availability_state if rows else None


def classify_paper_trade(db: Session, trade: PaperTrade) -> tuple[str, list[str], bool, str]:
    """Returns (primary_state, diagnostic_flags, is_historical_degraded, executability_label)."""
    flags: set[str] = set()

    pred = None
    if trade.signal_source == "MODEL":
        pred = db.get(PredictionLedger, trade.signal_id)

    snap = db.get(OddsSnapshot, trade.price_snapshot_id) if trade.price_snapshot_id else None

    is_historical_degraded = True
    if snap is not None and snap.polled_at is not None and snap.ingested_at is not None:
        is_historical_degraded = False

    if is_historical_degraded:
        if snap is not None:
            flags.add("provider_time_only_historical_row")
        gap_ref = trade.created_at - trade.signal_time if trade.created_at and trade.signal_time else None
        if gap_ref is not None and abs(gap_ref.total_seconds()) > HISTORICAL_CREATED_AT_GAP_S:
            flags.add("historical_created_at_not_event_time")

    match = db.get(Match, trade.match_id) if trade.match_id else None

    # signal-too-late check, independent of price outcome. v0.3.7D fix:
    # compare against actual/live start (from closing_records._actual_start,
    # the same earliest-phase='live'-odds-row convention used everywhere
    # else in this codebase), not Match.start_time directly -- real matches
    # were observed starting 7-60s (mean 29s) after their nominal
    # scheduled time, so using scheduled time alone mislabeled genuinely
    # pre-kickoff signals as too late.
    signal_too_late = False
    executability = UNKNOWN_START_TIME
    if pred is not None and match is not None and match.start_time is not None:
        executability = compute_executability(db, pred, match)
        actual_start, _ = _actual_start(db, match)
        if actual_start is not None and pred.prediction_time >= actual_start:
            signal_too_late = True

    # odds-history sufficiency (reuses the >=2-updates convention from
    # v0.3.7A Gate G4)
    all_snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == trade.match_id, OddsSnapshot.sportsbook == trade.sportsbook,
        OddsSnapshot.market == trade.market, OddsSnapshot.selection == trade.selection)).all()
    if len(all_snaps) < 2:
        flags.add("insufficient_odds_history")

    # floor / discount diagnostics (MODEL only -- friend picks don't have a
    # steam-derived floor)
    if pred is not None:
        if pred.maximum_entry_decimal is not None and pred.current_decimal is not None:
            if pred.maximum_entry_decimal == pred.current_decimal:
                flags.add("floor_equals_signal_price")
            if pred.maximum_entry_decimal >= pred.current_decimal:
                flags.add("no_discount_applied")
        # duplicate_signal: is this the canonical (latest prediction_time)
        # row for its (match_id, selection) group, or a superseded horizon?
        later = db.scalar(select(PredictionLedger.id).where(
            PredictionLedger.match_id == pred.match_id, PredictionLedger.selection == pred.selection,
            PredictionLedger.prediction_time > pred.prediction_time))
        if later is not None:
            flags.add("duplicate_signal")

    # direction alignment (only meaningful when a price was actually found)
    if pred is not None and trade.price_decimal is not None and pred.current_decimal is not None:
        delta = trade.price_decimal - pred.current_decimal
        predicted_shorten = (
            (pred.predicted_first_live_decimal is not None
             and pred.predicted_first_live_decimal < pred.current_decimal)
            or (pred.steam_probability or 0.5) >= 0.58
        )
        if predicted_shorten and delta < 0:
            flags.add("odds_moved_in_predicted_direction")
        elif not predicted_shorten and delta >= 0:
            flags.add("odds_moved_in_predicted_direction")
        else:
            flags.add("odds_moved_against_predicted_direction")

    # market-withdrawal candidate flags (forward data only -- historical
    # rows have no MarketAvailabilityRecord history and will simply not set
    # these, which is honest, not a bug)
    if match is not None:
        candidate = detect_withdrawal_relist_candidates(
            db, match, trade.sportsbook, trade.market, trade.selection)
        if candidate["withdrawn_candidate"]:
            flags.add("market_withdrawn_prekickoff_candidate")
        if candidate["relisted_candidate"]:
            flags.add("relisted_live_at_kickoff_candidate")

    nearby_state = None
    if match is not None:
        target_time = trade.signal_time + timedelta(seconds=trade.delay_seconds)
        nearby_state = _nearby_availability_state(
            db, trade.match_id, trade.sportsbook, trade.market, trade.selection, target_time)
    if nearby_state == EMPTY_PROVIDER_RESPONSE:
        flags.add("empty_provider_response")

    # staleness (provider-time vs system-time, kept as separate flags)
    if snap is not None:
        target_time = trade.signal_time + timedelta(seconds=trade.delay_seconds)
        provider_gap = abs((snap.collected_at - target_time).total_seconds()) if snap.collected_at else None
        if provider_gap is not None and provider_gap > STALE_THRESHOLD_S:
            flags.add("stale_provider_time")
        if snap.ingested_at is not None and snap.polled_at is not None:
            system_gap = abs((snap.ingested_at - snap.polled_at).total_seconds())
            if system_gap > STALE_THRESHOLD_S:
                flags.add("stale_system_time")

    # ---------------------------------------------------------- primary state
    if signal_too_late:
        primary = SIGNAL_TOO_LATE
    elif trade.price_decimal is None:
        if nearby_state == BOOK_MISSING_MARKET:
            primary = BOOK_MISSING_MARKET_STATE
        elif nearby_state in (ABSENT, EMPTY_PROVIDER_RESPONSE):
            primary = MARKET_UNAVAILABLE_AT_ENTRY
        else:
            primary = NO_DATA_AT_ENTRY
    elif trade.max_entry_decimal is not None and trade.price_decimal < trade.max_entry_decimal:
        primary = PRICE_BELOW_ENTRY_FLOOR
    elif trade.settlement_status in ("FILLED", "SETTLED"):
        primary = FILLED
    else:
        primary = UNKNOWN

    return primary, sorted(flags), is_historical_degraded, executability


def classify_and_store(db: Session, trade: PaperTrade) -> ExecutionClassification:
    primary, flags, degraded, executability = classify_paper_trade(db, trade)
    existing = db.scalar(select(ExecutionClassification).where(
        ExecutionClassification.paper_trade_id == trade.id))
    row = existing or ExecutionClassification(paper_trade_id=trade.id)
    row.primary_state = primary
    row.diagnostic_flags_json = json.dumps(flags)
    row.is_historical_degraded = degraded
    row.executability_label = executability
    if existing is None:
        db.add(row)
    db.commit()
    return row


def classify_all(db: Session) -> dict:
    """Classify every PaperTrade row that doesn't already have a current
    classification. Idempotent -- re-running updates existing rows in
    place rather than duplicating (unique constraint on paper_trade_id)."""
    trades = db.scalars(select(PaperTrade)).all()
    by_primary: dict[str, int] = {}
    by_executability: dict[str, int] = {}
    degraded_count = 0
    for t in trades:
        row = classify_and_store(db, t)
        by_primary[row.primary_state] = by_primary.get(row.primary_state, 0) + 1
        by_executability[row.executability_label] = by_executability.get(row.executability_label, 0) + 1
        if row.is_historical_degraded:
            degraded_count += 1
    return {
        "total_classified": len(trades),
        "by_primary_state": by_primary,
        "by_executability": by_executability,
        "historical_degraded_count": degraded_count,
        "forward_trustworthy_count": len(trades) - degraded_count,
    }
