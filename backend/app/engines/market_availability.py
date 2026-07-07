"""Market availability state tracking (v0.3.7B Section 3).

Pure change-only storage (the pre-existing OddsSnapshot / MarketEvent
"disappeared" pattern) cannot distinguish "we stopped polling" from "the
market genuinely vanished." This module writes a heartbeat row every poll
cycle -- even when odds are unchanged -- via MarketAvailabilityRecord, then
classifies availability state from that heartbeat history.

Per v0.3.7A: one friend-pick observation suggested FanDuel may hide/relist a
pregame market live. That is ONE observed case on a book we don't even poll
via BetsAPI (FanDuel isn't in our tracked sportsbooks) -- it is a monitored
flag and census target here, NOT a confirmed BetsAPI/bet365 mechanism. These
candidate states must not be used to build entry-scheduling logic; report
prevalence only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, MarketAvailabilityRecord, PollCycle

PRESENT = "PRESENT"
ABSENT = "ABSENT"
EMPTY_PROVIDER_RESPONSE = "EMPTY_PROVIDER_RESPONSE"
BOOK_MISSING_MARKET = "BOOK_MISSING_MARKET"
MARKET_WITHDRAWN_PREKICKOFF_CANDIDATE = "MARKET_WITHDRAWN_PREKICKOFF_CANDIDATE"
RELISTED_LIVE_AT_KICKOFF_CANDIDATE = "RELISTED_LIVE_AT_KICKOFF_CANDIDATE"
SUSPENDED_OR_HALTED = "SUSPENDED_OR_HALTED"
UNKNOWN = "UNKNOWN"

# Bounded pre-kickoff window used by the withdrawal/relist candidate rules.
PREKICKOFF_WITHDRAWAL_WINDOW_MIN = 10
CANDIDATE_PREVALENCE_MONITOR_PCT = 5.0
CANDIDATE_PREVALENCE_ESCALATE_PCT = 10.0


def classify_single_observation(*, call_succeeded: bool, payload_totally_empty: bool,
                                selection_present: bool,
                                book_has_ever_had_market: bool | None = None) -> str:
    """Classify ONE (match, sportsbook, market, selection) observation for
    ONE poll. Does not look at history -- that's detect_candidates()."""
    if not call_succeeded:
        return UNKNOWN
    if payload_totally_empty:
        return EMPTY_PROVIDER_RESPONSE
    if selection_present:
        return PRESENT
    if book_has_ever_had_market is False:
        return BOOK_MISSING_MARKET
    return ABSENT


def write_heartbeat(db: Session, *, match: Match, sportsbook: str, market: str,
                    selections_present: set[str], all_selections_tracked: tuple[str, ...],
                    call_succeeded: bool, payload_totally_empty: bool,
                    poll_cycle_id: int | None, source_ts: datetime | None,
                    observed_at: datetime, decimal_odds_by_selection: dict[str, float] | None = None) -> int:
    """Write one MarketAvailabilityRecord per tracked selection for this
    (match, sportsbook, market), even when odds are unchanged. Returns the
    number of rows written. Heartbeat granularity is per-selection for
    ML_3WAY (home/draw/away each tracked independently) since the
    withdrawal/relist rules care about a specific selection's presence."""
    decimal_odds_by_selection = decimal_odds_by_selection or {}
    s2k = None
    if match.start_time is not None:
        s2k = round((match.start_time - observed_at).total_seconds(), 1)
    written = 0
    ever_had = _book_has_ever_had_market(db, match.id, sportsbook, market)
    for sel in all_selections_tracked:
        state = classify_single_observation(
            call_succeeded=call_succeeded, payload_totally_empty=payload_totally_empty,
            selection_present=sel in selections_present,
            book_has_ever_had_market=ever_had)
        db.add(MarketAvailabilityRecord(
            observed_at=observed_at, match_id=match.id, sportsbook=sportsbook,
            market=market, selection=sel, poll_cycle_id=poll_cycle_id, source_ts=source_ts,
            availability_state=state, odds_changed=False,
            decimal_odds=decimal_odds_by_selection.get(sel), seconds_to_kickoff=s2k,
        ))
        written += 1
    db.commit()
    return written


def _book_has_ever_had_market(db: Session, match_id: int, sportsbook: str, market: str) -> bool | None:
    row = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match_id, MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market, MarketAvailabilityRecord.availability_state == PRESENT,
    ))
    if row is not None:
        return True
    any_row = db.scalar(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match_id, MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market))
    return False if any_row is not None else None


def detect_withdrawal_relist_candidates(db: Session, match: Match, sportsbook: str,
                                        market: str, selection: str) -> dict:
    """Scan heartbeat history for ONE (match, sportsbook, market, selection)
    and flag the two candidate patterns. Bounded, deterministic, read-only.

    MARKET_WITHDRAWN_PREKICKOFF_CANDIDATE: PRESENT earlier, then ABSENT
    continuously for the rest of the bounded pre-kickoff window
    (PREKICKOFF_WITHDRAWAL_WINDOW_MIN), with no PRESENT after that before
    match start.

    RELISTED_LIVE_AT_KICKOFF_CANDIDATE: the above, AND a PRESENT observation
    exists at or after match start (live relist)."""
    rows = db.scalars(select(MarketAvailabilityRecord).where(
        MarketAvailabilityRecord.match_id == match.id,
        MarketAvailabilityRecord.sportsbook == sportsbook,
        MarketAvailabilityRecord.market == market,
        MarketAvailabilityRecord.selection == selection,
    ).order_by(MarketAvailabilityRecord.observed_at)).all()
    if not rows:
        return {"withdrawn_candidate": False, "relisted_candidate": False, "n_observations": 0}

    window_start = match.start_time - timedelta(minutes=PREKICKOFF_WITHDRAWAL_WINDOW_MIN)
    pre_window = [r for r in rows if window_start <= r.observed_at < match.start_time]
    post_start = [r for r in rows if r.observed_at >= match.start_time]

    had_present_before_window = any(r.observed_at < window_start and r.availability_state == PRESENT
                                    for r in rows)
    all_absent_in_window = bool(pre_window) and all(r.availability_state == ABSENT for r in pre_window)
    withdrawn = had_present_before_window and all_absent_in_window

    relisted = withdrawn and any(r.availability_state == PRESENT for r in post_start)

    return {
        "withdrawn_candidate": withdrawn,
        "relisted_candidate": relisted,
        "n_observations": len(rows),
        "pre_window_observations": len(pre_window),
        "post_start_observations": len(post_start),
    }


def prevalence_report(db: Session) -> dict:
    """Aggregate withdrawal/relist candidate prevalence across all matches
    with heartbeat history. Report-only; does not gate anything."""
    match_ids = [mid for (mid,) in db.execute(
        select(MarketAvailabilityRecord.match_id).distinct()).all() if mid is not None]
    total_checked = 0
    withdrawn_count = 0
    relisted_count = 0
    by_book: dict[str, dict] = {}
    by_market: dict[str, dict] = {}

    for mid in match_ids:
        match = db.get(Match, mid)
        if match is None:
            continue
        combos = db.execute(select(MarketAvailabilityRecord.sportsbook, MarketAvailabilityRecord.market,
                                   MarketAvailabilityRecord.selection).where(
            MarketAvailabilityRecord.match_id == mid).distinct()).all()
        for sportsbook, market, selection in combos:
            total_checked += 1
            result = detect_withdrawal_relist_candidates(db, match, sportsbook, market, selection)
            b = by_book.setdefault(sportsbook, {"checked": 0, "withdrawn": 0, "relisted": 0})
            mkt = by_market.setdefault(market, {"checked": 0, "withdrawn": 0, "relisted": 0})
            b["checked"] += 1
            mkt["checked"] += 1
            if result["withdrawn_candidate"]:
                withdrawn_count += 1
                b["withdrawn"] += 1
                mkt["withdrawn"] += 1
            if result["relisted_candidate"]:
                relisted_count += 1
                b["relisted"] += 1
                mkt["relisted"] += 1

    pct = round(100 * withdrawn_count / total_checked, 2) if total_checked else None
    escalate = pct is not None and pct >= CANDIDATE_PREVALENCE_ESCALATE_PCT
    monitor_only = pct is not None and pct < CANDIDATE_PREVALENCE_MONITOR_PCT

    return {
        "total_match_book_market_selection_combos_checked": total_checked,
        "matches_with_heartbeat_data": len(match_ids),
        "withdrawn_prekickoff_candidate_count": withdrawn_count,
        "relisted_live_at_kickoff_candidate_count": relisted_count,
        "withdrawn_prevalence_pct": pct,
        "by_sportsbook": by_book,
        "by_market": by_market,
        "recommendation": (
            "NO HEARTBEAT DATA YET -- pending forward collection, not a verdict"
            if pct is None else
            "ESCALATE for execution-strategy review in a later release"
            if escalate else
            "keep as diagnostic flag only (below 5-10% prevalence threshold)"
            if monitor_only else
            "borderline (5-10%) -- keep monitoring, do not act yet"
        ),
        "note": ("BetsAPI/bet365 evidence only -- this is NOT the FanDuel manual "
                "friend-pick observation, which remains a separate, unconfirmed flag."),
    }
