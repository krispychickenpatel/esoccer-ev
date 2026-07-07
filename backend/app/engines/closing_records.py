"""Closing records and close quality (v0.3.7B Section 6).

Builds ClosingRecord rows, additive/new table, going forward. Never
imputes a close (a record is only created when a real OddsSnapshot row
exists to build it from) and never devigs an incomplete 3-way market
(all_three_outcomes_present is reported so downstream code can refuse to
devig when it's False -- this module does not devig at all).

Historical closes (built from pre-v0.3.7B OddsSnapshot rows with no system
timestamps) are always capped below HIGH quality -- "system age <=60s" is
literally uncomputable without polled_at/ingested_at, so DEGRADED historical
rows can reach MEDIUM at best. See v0.3.7A Gate G1/G4.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClosingRecord, Match, MarketAvailabilityRecord, OddsSnapshot
from .market_availability import ABSENT, PRESENT

PRE_KICKOFF = "PRE_KICKOFF"
PRE_SUSPENSION = "PRE_SUSPENSION"
LAST_AVAILABLE = "LAST_AVAILABLE"
LIVE_START_PROXY = "LIVE_START_PROXY"

HIGH, MEDIUM, LOW, INVALID = "HIGH", "MEDIUM", "LOW", "INVALID"

FRESHNESS_THRESHOLD_S = 60.0
PRE_KICKOFF_GAP_THRESHOLD_S = 300.0


def _actual_start(db: Session, match: Match) -> tuple:
    live_row = db.scalar(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.phase == "live",
    ).order_by(OddsSnapshot.collected_at))
    if live_row is not None:
        return live_row.collected_at, False
    return match.start_time, True


def _all_three_present(db: Session, match_id: int, sportsbook: str, market: str, at) -> bool:
    if market != "ML_3WAY":
        return True  # 2-way markets don't need the 3-way check
    window_lo, window_hi = at - timedelta(seconds=5), at + timedelta(seconds=5)
    rows = db.scalars(select(OddsSnapshot.selection).where(
        OddsSnapshot.match_id == match_id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market,
        OddsSnapshot.collected_at >= window_lo, OddsSnapshot.collected_at <= window_hi,
    ).distinct()).all()
    return {"home", "draw", "away"} <= set(rows)


def build_closing_record(db: Session, match: Match, sportsbook: str, market: str,
                         selection: str) -> ClosingRecord | None:
    """Never imputes: returns None if no real snapshot exists to close from."""
    actual_start, used_fallback = _actual_start(db, match)
    close_row = db.scalar(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.selection == selection,
        OddsSnapshot.collected_at <= actual_start,
    ).order_by(OddsSnapshot.collected_at.desc()))
    if close_row is None:
        return None

    has_system_ts = close_row.polled_at is not None and close_row.ingested_at is not None
    gap_s = (actual_start - close_row.collected_at).total_seconds()

    withdrawn_before_kickoff = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match.id, MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market, MarketAvailabilityRecord.selection == selection,
        MarketAvailabilityRecord.availability_state == ABSENT,
        MarketAvailabilityRecord.observed_at > close_row.collected_at,
        MarketAvailabilityRecord.observed_at < actual_start,
    ))

    if close_row.phase == "live":
        close_type = LIVE_START_PROXY
    elif withdrawn_before_kickoff is not None:
        close_type = PRE_SUSPENSION
    elif gap_s <= PRE_KICKOFF_GAP_THRESHOLD_S:
        close_type = PRE_KICKOFF
    else:
        close_type = LAST_AVAILABLE

    all_three = _all_three_present(db, match.id, sportsbook, market, close_row.collected_at)

    cutoff = actual_start - timedelta(minutes=5)
    updates_final_5m = len(db.scalars(select(OddsSnapshot.id).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.selection == selection,
        OddsSnapshot.collected_at >= cutoff, OddsSnapshot.collected_at <= actual_start,
    )).all())

    opening_row = db.scalar(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.selection == selection,
    ).order_by(OddsSnapshot.collected_at))
    updates_between = len(db.scalars(select(OddsSnapshot.id).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.selection == selection,
        OddsSnapshot.collected_at >= opening_row.collected_at,
        OddsSnapshot.collected_at <= close_row.collected_at,
    )).all()) if opening_row else 0

    avail_row = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match.id, MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market, MarketAvailabilityRecord.selection == selection,
        MarketAvailabilityRecord.observed_at >= close_row.collected_at - timedelta(seconds=90),
        MarketAvailabilityRecord.observed_at <= close_row.collected_at + timedelta(seconds=90),
    ).order_by(MarketAvailabilityRecord.observed_at.desc()))
    market_available_at_close = (avail_row.availability_state == PRESENT) if avail_row else None

    flags = []
    if not has_system_ts:
        flags.append("DEGRADED_NO_SYSTEM_TIMESTAMPS")
    if used_fallback:
        flags.append("ACTUAL_START_USED_SCHEDULED_KICKOFF_FALLBACK")

    system_age_ok = has_system_ts and gap_s <= FRESHNESS_THRESHOLD_S
    if not has_system_ts:
        quality = MEDIUM if (all_three and market_available_at_close) else \
                  LOW if (all_three or market_available_at_close) else INVALID
    elif all_three and system_age_ok and updates_final_5m >= 2 and market_available_at_close:
        quality = HIGH
    elif all_three and market_available_at_close:
        quality = MEDIUM
    elif all_three or market_available_at_close:
        quality = LOW
    else:
        quality = INVALID

    existing = db.scalar(select(ClosingRecord).where(
        ClosingRecord.match_id == match.id, ClosingRecord.sportsbook == sportsbook,
        ClosingRecord.market == market, ClosingRecord.selection == selection))
    row = existing or ClosingRecord(match_id=match.id, sportsbook=sportsbook, market=market,
                                    selection=selection)
    row.provider_event_id = close_row.provider_event_id
    row.close_source_ts = close_row.collected_at
    row.close_polled_at = close_row.polled_at
    row.close_ingested_at = close_row.ingested_at
    row.close_price_decimal = close_row.decimal_odds
    row.close_american = close_row.american_odds
    row.close_type = close_type
    row.close_quality = quality
    row.all_three_outcomes_present = all_three
    row.updates_final_5m_count = updates_final_5m
    row.updates_between_entry_and_close_count = updates_between
    row.market_available_at_close = market_available_at_close
    row.flags_json = json.dumps(flags)
    if existing is None:
        db.add(row)
    db.commit()
    return row


def build_all(db: Session, sportsbook: str = "bet365", market: str = "ML_3WAY") -> dict:
    matches = db.scalars(select(Match).where(Match.home_score.is_not(None))).all()
    created = 0
    quality_counts: dict[str, int] = {}
    for m in matches:
        for selection in (("home", "draw", "away") if market == "ML_3WAY" else ("home", "away")):
            row = build_closing_record(db, m, sportsbook, market, selection)
            if row is not None:
                created += 1
                quality_counts[row.close_quality] = quality_counts.get(row.close_quality, 0) + 1
    return {"matches_checked": len(matches), "closing_records_built": created,
           "by_quality": quality_counts}
