"""Friend Pick Ledger + Scoring (v0.3.6 Modules 1-2).

Treats a friend's pick as a timestamped signal source, never as truth.

Immutability: a FriendPick row is never edited after creation. Corrections
are new rows pointing at the original via corrects_pick_id (same pattern as
PredictionLedger's freeze/never-edit rule, verified by immutable_hash).

Leakage anchor: effective_known_at = max(pick_timestamp, created_at). A
backfilled pick (created_at far after pick_timestamp) can never be scored,
priced, or compared as if the platform knew about it before it actually
entered the system -- every odds lookup and every comparison below uses
effective_known_at, never pick_timestamp.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (BookmakerCoverage, FriendPick, FriendPickScore, Match,
                      OddsSnapshot, PredictionLedger, PredictionReality, Settings)
from . import odds_math
from .execution_pricing import latest_snapshot_for, price_at_delay
from .execution_strategy import classify_execution_mode
from .identity import canonical_name
from .steam import steam_prediction_for_snapshot

BACKFILL_THRESHOLD_SECONDS = 120
RESOLUTION_KICKOFF_TOLERANCE = timedelta(minutes=10)
# v0.3.6.1: a pick logged more than this long after its own kickoff is very
# unlikely to be a real live pick -- surfaced as a read-only diagnostic
# (likely_test_artifact in pick_out), never used to silently exclude
# anything or mutate data.
LIKELY_TEST_ARTIFACT_THRESHOLD = timedelta(minutes=60)

# v0.3.6.1: RESULT_RIGHT_NO_EDGE renamed to RESULT_RIGHT_NO_MARKET_EDGE per
# audit fix spec. The dead "OK" bucket from v0.3.6 is gone -- every scored
# pick now lands in exactly one of these 7, see _classify_friend_error_bucket.
ERROR_BUCKETS = (
    "CORRECT_SIDE_BAD_PRICE", "WRONG_SIDE", "STEAM_RIGHT_RESULT_WRONG",
    "RESULT_RIGHT_NO_MARKET_EDGE", "MISSED_EXECUTION_WINDOW", "DATA_UNAVAILABLE",
    "BOOK_UNAVAILABLE",
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _freeze_payload_v1(pick_timestamp, effective_known_at, home_name, away_name,
                       pick_side, odds_at_pick_decimal, book_seen, league, kickoff_time) -> dict:
    """Original v0.3.6 hash payload. Kept byte-for-byte so pre-v0.3.6.1 rows
    still verify -- never used for new rows."""
    return {
        "pick_timestamp": pick_timestamp.isoformat(),
        "effective_known_at": effective_known_at.isoformat(),
        "home_name": home_name, "away_name": away_name,
        "pick_side": pick_side, "odds_at_pick_decimal": odds_at_pick_decimal,
        "book_seen": book_seen, "league": league,
        "kickoff_time": kickoff_time.isoformat() if kickoff_time else None,
    }


def _freeze_payload_v2(pick_timestamp, effective_known_at, home_name, away_name,
                       pick_side, odds_at_pick_decimal, odds_at_pick_american,
                       book_seen, league, kickoff_time, reason, confidence,
                       provider_event_id) -> dict:
    """v0.3.6.1: covers every user-entered original pick field, so tampering
    with reason/confidence/odds_at_pick_american/provider_event_id is
    detectable too. Used for every row created from v0.3.6.1 onward."""
    return {
        "pick_timestamp": pick_timestamp.isoformat(),
        "effective_known_at": effective_known_at.isoformat(),
        "home_name": home_name, "away_name": away_name,
        "pick_side": pick_side, "odds_at_pick_decimal": odds_at_pick_decimal,
        "odds_at_pick_american": odds_at_pick_american,
        "book_seen": book_seen, "league": league,
        "kickoff_time": kickoff_time.isoformat() if kickoff_time else None,
        "reason": reason, "confidence": confidence,
        "provider_event_id": provider_event_id,
    }


# Backward-compat alias -- some call sites/tests still refer to the
# original name. Always the v1 (original) payload shape.
_freeze_payload = _freeze_payload_v1


def _freeze_hash(payload: dict) -> str:
    return hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest()


def _compute_execution_mode(db: Session, pick: FriendPick, match: Match) -> None:
    """Best-effort execution-mode classification for a friend pick, run once
    at resolution time. There is no model behind a friend's tip, so model_prob
    is proxied by the de-vigged (fair) market-implied probability of the
    picked side at effective_known_at -- a real, observable number, not a
    fabricated one. Silently leaves execution_mode=None if there isn't
    enough market data yet (no snapshots near the pick)."""
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match.id, OddsSnapshot.market == "ML_3WAY",
        OddsSnapshot.collected_at <= pick.effective_known_at,
    ).order_by(OddsSnapshot.collected_at.desc())).all()
    latest_per_sel: dict[str, OddsSnapshot] = {}
    for s in snaps:
        latest_per_sel.setdefault(s.selection, s)
    pick_snap = latest_per_sel.get(pick.pick_side)
    if pick_snap is None or len(latest_per_sel) < 2:
        return
    implied = {sel: odds_math.implied_prob(sn.decimal_odds) for sel, sn in latest_per_sel.items()}
    fair = odds_math.remove_vig(list(implied.values()))
    fair_by_sel = dict(zip(implied.keys(), fair))
    model_prob = fair_by_sel.get(pick.pick_side, implied[pick.pick_side])

    settings = db.get(Settings, 1)
    steam = steam_prediction_for_snapshot(db, match, pick_snap, settings, as_of=pick.effective_known_at)
    result = classify_execution_mode(
        db, current_decimal=pick_snap.decimal_odds,
        predicted_first_live_decimal=steam.get("predicted_first_live_decimal"),
        max_entry_decimal=steam.get("maximum_entry_decimal"),
        model_prob=model_prob, min_ev_pct=(settings.min_ev_pct if settings else 5.0),
        steam_probability=steam.get("steam_probability", 0.5),
    )
    pick.execution_mode = result["execution_mode"]
    pick.execution_reason_codes_json = _dumps(result["reason_codes"])


def _try_auto_resolve(db: Session, pick: FriendPick) -> None:
    """Zero or multiple candidates -> stays PENDING. Exactly one -> RESOLVED."""
    if not pick.kickoff_time:
        pick.resolution_status = "PENDING"
        return
    home_c = canonical_name(pick.home_name)
    away_c = canonical_name(pick.away_name)
    window_lo = pick.kickoff_time - RESOLUTION_KICKOFF_TOLERANCE
    window_hi = pick.kickoff_time + RESOLUTION_KICKOFF_TOLERANCE
    candidates = db.scalars(select(Match).where(
        Match.start_time >= window_lo, Match.start_time <= window_hi,
    )).all()
    matches = []
    for m in candidates:
        if pick.league and pick.league.strip():
            if pick.league.lower() not in (m.league or "").lower() and (m.league or "").lower() not in pick.league.lower():
                continue
        if canonical_name(m.home_player.name) == home_c and canonical_name(m.away_player.name) == away_c:
            matches.append(m)
    if len(matches) == 1:
        pick.match_id = matches[0].id
        pick.resolution_status = "RESOLVED"
        try:
            _compute_execution_mode(db, pick, matches[0])
        except Exception:
            pass  # execution-mode is best-effort; resolution must not fail because of it
    else:
        pick.resolution_status = "PENDING"


def create_friend_pick(db: Session, payload: dict) -> FriendPick:
    now = _now()
    pick_timestamp = payload.get("pick_timestamp") or now
    if isinstance(pick_timestamp, str):
        pick_timestamp = datetime.fromisoformat(pick_timestamp)
    effective_known_at = max(pick_timestamp, now)
    is_backfilled = (now - pick_timestamp).total_seconds() > BACKFILL_THRESHOLD_SECONDS

    kickoff_time = payload.get("kickoff_time")
    if isinstance(kickoff_time, str):
        kickoff_time = datetime.fromisoformat(kickoff_time)

    odds_dec = payload["odds_at_pick_decimal"]
    odds_am = payload.get("odds_at_pick_american")
    if odds_am is None:
        odds_am = odds_math.decimal_to_american(odds_dec)

    # v0.3.6.1: all new rows hash under v2 (covers reason/confidence/
    # odds_at_pick_american/provider_event_id too). Existing pre-v0.3.6.1
    # rows keep their original v1 hash -- verify_integrity() accepts either.
    frozen = _freeze_payload_v2(pick_timestamp, effective_known_at,
                                payload.get("home_name", ""), payload.get("away_name", ""),
                                payload["pick_side"], odds_dec, odds_am,
                                payload.get("book_seen", ""), payload.get("league", ""),
                                kickoff_time, payload.get("reason", ""),
                                payload.get("confidence"), payload.get("provider_event_id"))

    pick = FriendPick(
        created_at=now, pick_timestamp=pick_timestamp, effective_known_at=effective_known_at,
        is_backfilled=is_backfilled, provider_event_id=payload.get("provider_event_id"),
        league=payload.get("league", ""), home_name=payload.get("home_name", ""),
        away_name=payload.get("away_name", ""), kickoff_time=kickoff_time,
        pick_side=payload["pick_side"], odds_at_pick_american=odds_am,
        odds_at_pick_decimal=odds_dec, book_seen=payload.get("book_seen", ""),
        reason=payload.get("reason", ""), confidence=payload.get("confidence"),
        resolution_status="PENDING", scoring_status="pending",
        immutable_hash=_freeze_hash(frozen),
    )
    _try_auto_resolve(db, pick)
    db.add(pick)
    db.commit()
    return pick


def resolve_friend_pick(db: Session, pick_id: int, match_id: int) -> FriendPick | None:
    pick = db.get(FriendPick, pick_id)
    if not pick:
        return None
    m = db.get(Match, match_id)
    if not m:
        return None
    pick.match_id = match_id
    pick.resolution_status = "RESOLVED"
    try:
        _compute_execution_mode(db, pick, m)
    except Exception:
        pass
    db.commit()
    return pick


def auto_resolve_pending(db: Session) -> dict:
    """Cheap, DB-only re-scan of PENDING picks. Safe to run on every poller
    tick (no API calls) -- called throttled from services/poller.py."""
    pending = db.scalars(select(FriendPick).where(FriendPick.resolution_status == "PENDING")).all()
    resolved = 0
    for pick in pending:
        before = pick.resolution_status
        _try_auto_resolve(db, pick)
        if pick.resolution_status == "RESOLVED" and before != "RESOLVED":
            resolved += 1
    if pending:
        db.commit()
    return {"checked": len(pending), "resolved": resolved}


def correct_friend_pick(db: Session, pick_id: int, payload: dict) -> FriendPick | None:
    """Corrections are NEW rows. The original is never edited."""
    original = db.get(FriendPick, pick_id)
    if not original:
        return None
    merged = {
        "pick_timestamp": payload.get("pick_timestamp", original.pick_timestamp),
        "home_name": payload.get("home_name", original.home_name),
        "away_name": payload.get("away_name", original.away_name),
        "league": payload.get("league", original.league),
        "kickoff_time": payload.get("kickoff_time", original.kickoff_time),
        "pick_side": payload.get("pick_side", original.pick_side),
        "odds_at_pick_decimal": payload.get("odds_at_pick_decimal", original.odds_at_pick_decimal),
        "odds_at_pick_american": payload.get("odds_at_pick_american"),
        "book_seen": payload.get("book_seen", original.book_seen),
        "reason": payload.get("reason", original.reason),
        "confidence": payload.get("confidence", original.confidence),
        "provider_event_id": payload.get("provider_event_id", original.provider_event_id),
    }
    correction = create_friend_pick(db, merged)
    correction.corrects_pick_id = original.id
    db.commit()
    return correction


def favorite_selection(db: Session, match_id: int, sportsbook: str, market: str, at: datetime) -> str | None:
    """Market-only baseline: the lowest pre-kickoff decimal price = favorite."""
    snaps = db.scalars(select(OddsSnapshot).where(
        OddsSnapshot.match_id == match_id, OddsSnapshot.sportsbook == sportsbook,
        OddsSnapshot.market == market, OddsSnapshot.collected_at <= at,
        OddsSnapshot.phase == "pre_match",
    ).order_by(OddsSnapshot.collected_at.desc())).all()
    latest_per_sel: dict[str, OddsSnapshot] = {}
    for s in snaps:
        latest_per_sel.setdefault(s.selection, s)
    if not latest_per_sel:
        return None
    return min(latest_per_sel.items(), key=lambda kv: kv[1].decimal_odds)[0]


def _reality_row_for(db: Session, match_id: int, selection: str, market: str = "ML_3WAY") -> PredictionReality | None:
    s = db.get(Settings, 1)
    preferred_books = []
    if s and s.sportsbooks_tracked:
        try:
            preferred_books = json.loads(s.sportsbooks_tracked)
        except Exception:
            preferred_books = []
    rows = db.scalars(select(PredictionReality).where(
        PredictionReality.match_id == match_id, PredictionReality.selection == selection,
        PredictionReality.market == market,
    )).all()
    if not rows:
        return None
    for book in preferred_books:
        for r in rows:
            if r.sportsbook == book:
                return r
    return rows[0]


def _check_book_coverage(db: Session, book_seen: str | None) -> str:
    """Never guesses. Returns:
    - "verified"    -- book_seen matches a scanned BookmakerCoverage row
                       with status WORKS.
    - "unavailable" -- book_seen matches a scanned row with any other
                       status (EMPTY/BROKEN/UNKNOWN) -- genuine, proven.
    - "unknown"     -- book_seen is blank, no scan has EVER run, or this
                       specific book was never scanned. Not proof of
                       unavailability -- callers must not treat this as
                       BOOK_UNAVAILABLE."""
    if not book_seen or not book_seen.strip():
        return "unknown"
    rows = db.scalars(select(BookmakerCoverage)).all()
    if not rows:
        return "unknown"  # no scan has ever run -- never guess
    needle = book_seen.lower()
    for r in rows:
        if r.source_name.lower() in needle:
            return "verified" if r.status == "WORKS" else "unavailable"
    return "unknown"  # this specific book was never scanned


def _classify_friend_error_bucket(*, book_check: str, winner_correct: bool | None,
                                  steam_direction_correct: bool | None,
                                  proxy_clv_pct: float | None,
                                  entry_price_survived: bool | None) -> str:
    """Pure decision table -- exhaustive, mutually exclusive, exactly the 7
    spec'd buckets. No 'OK' escape hatch (v0.3.6 bug, fixed in v0.3.6.1)."""
    if book_check == "unavailable":
        return "BOOK_UNAVAILABLE"
    if winner_correct is None:
        return "DATA_UNAVAILABLE"
    if not winner_correct:
        # Result wrong. If the market agreed with the pick pre-result
        # (price shortened), the read was right and the result wasn't --
        # otherwise it was just the wrong side, full stop.
        return "STEAM_RIGHT_RESULT_WRONG" if steam_direction_correct is True else "WRONG_SIDE"
    # winner_correct is True from here.
    if entry_price_survived is False:
        return "MISSED_EXECUTION_WINDOW"
    if proxy_clv_pct is not None and proxy_clv_pct < 0:
        return "CORRECT_SIDE_BAD_PRICE"
    # Won, price data either fine or unavailable, nothing else to flag.
    # There is no 8th "won with a real edge" bucket in the spec -- this is
    # the correct default per the closed 7-bucket contract.
    return "RESULT_RIGHT_NO_MARKET_EDGE"


def score_friend_pick(db: Session, pick: FriendPick) -> FriendPickScore | None:
    """Only RESOLVED picks with a known result can be scored. Never uses
    data timestamped after pick.effective_known_at for anything that claims
    to represent "what was knowable at pick time" (odds_at_pick is already
    friend-supplied and immutable; everything computed here is scored
    AFTER the fact using real reality/result data, which is fine -- scoring
    happens later by definition, it just must never pretend the pick itself
    was made later/earlier than effective_known_at)."""
    if pick.resolution_status != "RESOLVED" or not pick.match_id:
        return None
    match = db.get(Match, pick.match_id)
    if not match:
        return None

    existing = db.scalar(select(FriendPickScore).where(FriendPickScore.friend_pick_id == pick.id))
    settings = db.get(Settings, 1)
    stake_units = 1.0
    stake_usd = (settings.paper_stake_usd if settings else 100.0)

    book_check = _check_book_coverage(db, pick.book_seen)
    reality = _reality_row_for(db, match.id, pick.pick_side)
    if match.home_score is None or match.away_score is None:
        if book_check == "unavailable":
            error_bucket = "BOOK_UNAVAILABLE"
        elif reality is None:
            error_bucket = "DATA_UNAVAILABLE"
        else:
            error_bucket = "MISSED_EXECUTION_WINDOW" if "missing_first_live" in json.loads(reality.warnings_json or "[]") else "DATA_UNAVAILABLE"
        row = existing or FriendPickScore(friend_pick_id=pick.id)
        row.error_bucket = error_bucket
        row.details_json = _dumps({"reason": "match result not yet available", "book_check": book_check})
        row.scored_at = _now()
        if existing is None:
            db.add(row)
        db.commit()
        return row

    winner_correct = (match.winner == pick.pick_side) if match.winner else None
    steam_direction_correct = None
    post_pick_movement_cents = None
    proxy_clv_pct = None
    entry_price_survived = None

    if reality is not None and reality.last_pre_decimal is not None and reality.first_live_decimal is not None:
        steam_direction_correct = bool(reality.actual_shortened)
        post_pick_movement_cents = round((reality.first_live_decimal - pick.odds_at_pick_decimal) * 100, 1)
        closing = latest_snapshot_for(db, match.id, reality.sportsbook, "ML_3WAY", pick.pick_side)
        if closing is not None:
            try:
                proxy_clv_pct = round(odds_math.clv_pct(pick.odds_at_pick_decimal, closing.decimal_odds) * 100, 2)
            except (ValueError, ZeroDivisionError):
                proxy_clv_pct = None
        entry_price_survived = reality.first_live_decimal >= pick.odds_at_pick_decimal

    error_bucket = _classify_friend_error_bucket(
        book_check=book_check, winner_correct=winner_correct,
        steam_direction_correct=steam_direction_correct,
        proxy_clv_pct=proxy_clv_pct, entry_price_survived=entry_price_survived)

    paper_pl_usd = None
    if winner_correct is not None:
        if winner_correct:
            paper_pl_usd = round(stake_usd * (pick.odds_at_pick_decimal - 1), 2)
        else:
            paper_pl_usd = round(-stake_usd, 2)

    # vs model: same match/market/selection frozen PredictionLedger row at
    # the nearest horizon at-or-before effective_known_at. Never freeze one
    # retroactively -- if none exists, comparison is NOT_AVAILABLE.
    model_row = db.scalar(select(PredictionLedger).where(
        PredictionLedger.match_id == match.id, PredictionLedger.market == "ML_3WAY",
        PredictionLedger.prediction_time <= pick.effective_known_at,
    ).order_by(PredictionLedger.prediction_time.desc()))
    vs_model = "NOT_AVAILABLE"
    if model_row is not None and winner_correct is not None and match.winner:
        model_correct = model_row.predicted_winner == match.winner
        friend_correct = pick.pick_side == match.winner
        vs_model = "TIE" if model_correct == friend_correct else ("BEAT" if friend_correct else "LOST")

    vs_baseline = "NOT_AVAILABLE"
    if reality is not None:
        favorite = favorite_selection(db, match.id, reality.sportsbook, "ML_3WAY", pick.effective_known_at)
        if favorite is not None and match.winner:
            baseline_correct = favorite == match.winner
            friend_correct = pick.pick_side == match.winner
            vs_baseline = "TIE" if baseline_correct == friend_correct else ("BEAT" if friend_correct else "LOST")

    row = existing or FriendPickScore(friend_pick_id=pick.id)
    row.winner_correct = winner_correct
    row.steam_direction_correct = steam_direction_correct
    row.first_live_movement_error_cents = None
    row.post_pick_movement_cents = post_pick_movement_cents
    row.proxy_clv_pct = proxy_clv_pct
    row.entry_price_survived = entry_price_survived
    row.paper_stake = stake_units
    row.paper_pl_usd = paper_pl_usd
    row.vs_model_comparison = vs_model
    row.vs_baseline_comparison = vs_baseline
    row.error_bucket = error_bucket
    row.details_json = _dumps({
        "reality_available": reality is not None,
        "book_used": reality.sportsbook if reality else None,
        "book_check": book_check,
    })
    row.scored_at = _now()
    if existing is None:
        db.add(row)
    pick.scoring_status = "scored"
    db.commit()
    return row


def score_all_resolved(db: Session) -> dict:
    picks = db.scalars(select(FriendPick).where(
        FriendPick.resolution_status == "RESOLVED", FriendPick.corrects_pick_id.is_(None),
    )).all()
    scored = 0
    for p in picks:
        before = p.scoring_status
        result = score_friend_pick(db, p)
        if result is not None and p.scoring_status == "scored" and before != "scored":
            scored += 1
    return {"checked": len(picks), "newly_scored": scored}


_V1_HASH_FIELDS = ["pick_timestamp", "effective_known_at", "home_name", "away_name",
                  "pick_side", "odds_at_pick_decimal", "book_seen", "league", "kickoff_time"]
_V2_HASH_FIELDS = _V1_HASH_FIELDS + ["odds_at_pick_american", "reason", "confidence", "provider_event_id"]


def verify_integrity(db: Session, limit: int = 5000) -> dict:
    """Recompute each FriendPick's hash and compare with immutable_hash.
    Rows created before v0.3.6.1 were hashed under the narrower v1 payload
    (see _freeze_payload_v1); rows from v0.3.6.1 onward use v2 (covers
    reason/confidence/odds_at_pick_american/provider_event_id too). A row
    is valid if it matches EITHER reconstruction -- this never invalidates
    existing rows just because the hash contract grew."""
    rows = db.scalars(select(FriendPick).order_by(FriendPick.id).limit(limit)).all()
    invalid_ids: list[int] = []
    for p in rows:
        v2 = _freeze_payload_v2(p.pick_timestamp, p.effective_known_at, p.home_name, p.away_name,
                                p.pick_side, p.odds_at_pick_decimal, p.odds_at_pick_american,
                                p.book_seen, p.league, p.kickoff_time, p.reason, p.confidence,
                                p.provider_event_id)
        v1 = _freeze_payload_v1(p.pick_timestamp, p.effective_known_at, p.home_name, p.away_name,
                                p.pick_side, p.odds_at_pick_decimal, p.book_seen, p.league,
                                p.kickoff_time)
        if _freeze_hash(v2) == p.immutable_hash or _freeze_hash(v1) == p.immutable_hash:
            continue
        invalid_ids.append(p.id)
    return {
        "checked": len(rows),
        "valid": len(rows) - len(invalid_ids),
        "invalid": len(invalid_ids),
        "invalid_ids": invalid_ids[:50],
        "hash_fields_v1": _V1_HASH_FIELDS,
        "hash_fields_v2": _V2_HASH_FIELDS,
        "caveat": ("Rows created before v0.3.6.1 were hashed under the v1 payload (fewer "
                  "fields) and are verified against that shape; rows from v0.3.6.1 onward "
                  "use v2. A row is valid if it matches either shape -- this is intentional "
                  "and does not weaken tamper detection for any individual row, since each "
                  "row only ever had one real original hash."),
    }


def report(db: Session) -> dict:
    picks = db.scalars(select(FriendPick)).all()
    scores = {s.friend_pick_id: s for s in db.scalars(select(FriendPickScore)).all()}
    by_status = {"PENDING": 0, "RESOLVED": 0, "UNRESOLVED": 0}
    for p in picks:
        by_status[p.resolution_status] = by_status.get(p.resolution_status, 0) + 1
    scored_rows = [scores[p.id] for p in picks if p.id in scores]
    n_scored = len(scored_rows)
    winner_acc = None
    steam_acc = None
    total_pl = None
    avg_clv = None
    if n_scored:
        w = [r.winner_correct for r in scored_rows if r.winner_correct is not None]
        s_ = [r.steam_direction_correct for r in scored_rows if r.steam_direction_correct is not None]
        pl = [r.paper_pl_usd for r in scored_rows if r.paper_pl_usd is not None]
        clv = [r.proxy_clv_pct for r in scored_rows if r.proxy_clv_pct is not None]
        winner_acc = round(sum(w) / len(w), 3) if w else None
        steam_acc = round(sum(s_) / len(s_), 3) if s_ else None
        total_pl = round(sum(pl), 2) if pl else None
        avg_clv = round(sum(clv) / len(clv), 2) if clv else None
    bucket_counts: dict[str, int] = {}
    for r in scored_rows:
        bucket_counts[r.error_bucket] = bucket_counts.get(r.error_bucket, 0) + 1
    beat_baseline = sum(1 for r in scored_rows if r.vs_baseline_comparison == "BEAT")
    lost_baseline = sum(1 for r in scored_rows if r.vs_baseline_comparison == "LOST")
    return {
        "total_picks": len(picks),
        "by_resolution_status": by_status,
        "pending_scoring": sum(1 for p in picks if p.scoring_status != "scored" and p.resolution_status == "RESOLVED"),
        "scored": n_scored,
        "winner_accuracy": winner_acc,
        "steam_direction_accuracy": steam_acc,
        "total_paper_pl_usd": total_pl,
        "avg_proxy_clv_pct": avg_clv,
        "vs_baseline": {"beat": beat_baseline, "lost": lost_baseline,
                       "tie_or_na": n_scored - beat_baseline - lost_baseline},
        "error_buckets": bucket_counts,
    }


def pick_out(db: Session, p: FriendPick) -> dict:
    score = db.scalar(select(FriendPickScore).where(FriendPickScore.friend_pick_id == p.id))

    # v0.3.6.1 Fix 6: never pretend book-specific execution was verified.
    # scoring_price_source is whichever book's reality data actually backed
    # the score (details_json.book_used); is_reference_feed_proxy is true
    # whenever that differs from what book_seen literally claims (which,
    # today, is effectively always -- we only track bet365 reality data).
    scoring_price_source = None
    is_reference_feed_proxy = None
    if score is not None:
        try:
            details = json.loads(score.details_json or "{}")
        except json.JSONDecodeError:
            details = {}
        scoring_price_source = details.get("book_used")
        if scoring_price_source:
            is_reference_feed_proxy = scoring_price_source.lower() not in (p.book_seen or "").lower()
    book_verified_for_execution = False
    if p.book_seen:
        cov = db.scalars(select(BookmakerCoverage)).all()
        needle = p.book_seen.lower()
        for c in cov:
            if c.source_name.lower() in needle:
                book_verified_for_execution = c.status == "WORKS" and c.execution_candidate
                break

    # v0.3.6.1 Fix 7: read-only heuristic, never stored, never mutates data,
    # never feeds into gates automatically -- surfaced so Kris can see which
    # picks look like test artifacts entered long after the match concluded.
    likely_test_artifact = bool(
        p.kickoff_time and (p.created_at - p.kickoff_time) > LIKELY_TEST_ARTIFACT_THRESHOLD)

    return {
        "id": p.id, "created_at": p.created_at.isoformat(),
        "pick_timestamp": p.pick_timestamp.isoformat(),
        "effective_known_at": p.effective_known_at.isoformat(),
        "is_backfilled": p.is_backfilled, "match_id": p.match_id,
        "provider_event_id": p.provider_event_id, "league": p.league,
        "home_name": p.home_name, "away_name": p.away_name,
        "kickoff_time": p.kickoff_time.isoformat() if p.kickoff_time else None,
        "pick_side": p.pick_side, "odds_at_pick_american": p.odds_at_pick_american,
        "odds_at_pick_decimal": p.odds_at_pick_decimal, "book_seen": p.book_seen,
        "reason": p.reason, "confidence": p.confidence,
        "resolution_status": p.resolution_status, "scoring_status": p.scoring_status,
        "corrects_pick_id": p.corrects_pick_id, "immutable_hash": p.immutable_hash,
        "execution_mode": p.execution_mode,
        "execution_reason_codes": json.loads(p.execution_reason_codes_json or "[]"),
        "scoring_price_source": scoring_price_source,
        "is_reference_feed_proxy": is_reference_feed_proxy,
        "book_verified_for_execution": book_verified_for_execution,
        "likely_test_artifact": likely_test_artifact,
        "score": None if score is None else {
            "winner_correct": score.winner_correct,
            "steam_direction_correct": score.steam_direction_correct,
            "post_pick_movement_cents": score.post_pick_movement_cents,
            "proxy_clv_pct": score.proxy_clv_pct,
            "entry_price_survived": score.entry_price_survived,
            "paper_pl_usd": score.paper_pl_usd,
            "vs_model_comparison": score.vs_model_comparison,
            "vs_baseline_comparison": score.vs_baseline_comparison,
            "error_bucket": score.error_bucket,
        },
    }
