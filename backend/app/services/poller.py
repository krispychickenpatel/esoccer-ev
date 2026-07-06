"""Odds Polling Service (spec).

Adaptive cadence:
    >10 min out      : every 60 s
    2-10 min out     : every 15 s
    <2 min to KO     : every 3 s
    0-30 s after live: every 1 s (if provider rate limits allow)
Ships OFF (settings.poller_enabled=False) and idles without a provider (D8).

Per tick per tracked match:
- fetch odds via provider, store snapshot with phase + seconds_to_kickoff
- detect: first live tick, line jumps (>=0.10 decimal), market disappear /
  reappear, odds crossing a recommendation's minimum threshold
- write MarketEvent rows so the Movement/Pick engines and alerts can react

v0.3.5 additions (Provider Execution Fix):
- match selection is priority-ordered (live-missing-first-live > near-kickoff
  > already-tracked > distant) and capped per tick, instead of treating every
  tracked match equally -- see _match_priority()/MAX_MATCHES_PER_TICK.
- ended-results ingestion (ingest_ended_results) feeds finished scores back
  into Match rows so Prediction Lab scoring can actually run.
- performance_report() surfaces loop timing, call volume, and first-live
  latency percentiles for validation sessions.
- Settings.validation_mode_enabled narrows tracking to the N soonest-kickoff
  matches for a clean first-live latency test.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..database import SessionLocal
from ..models import (Match, MarketEvent, OddsSnapshot, PredictionLedger,
                      PredictionReality, RawProviderResponse, Recommendation,
                      Settings)
from ..routers.data import upsert_match

STATUS = {"running": False, "ticks": 0, "snapshots_written": 0,
          "events_written": 0, "last_tick": None, "provider": None,
          "note": "poller idle",
          # v0.3.5 metrics/state
          "loop_duration_s": None, "active_tracked_matches": 0,
          "first_live_candidates": 0, "validation_mode": False,
          "result_ingestion": None}

# match_id -> last time we actually called provider.fetch_odds for it.
# Without this, cadence_seconds() only controls the loop's sleep, not whether
# any individual match gets re-fetched -- every match would get hit every loop
# tick regardless of bucket, silently blowing the budget the cadence table is
# meant to protect (D17).
_LAST_POLLED: dict[int, datetime] = {}

# D27: last time fetch_upcoming() was called to discover new matches.
# Module-level, shared across poll_loop iterations, throttled to once/60s.
_LAST_DISCOVERY: datetime | None = None

# Last time the Prediction Lab cycle ran (throttled to once/20s so lab
# bookkeeping can never starve first-live odds capture).
_LAST_LAB_CYCLE: datetime | None = None

# v0.3.5: last time ended-results ingestion ran (throttled to once/45s --
# esoccer matches don't finish faster than that, and this keeps the extra
# /v3/events/ended call cheap against the hourly quota).
_LAST_RESULTS_INGEST: datetime | None = None

# v0.3.6: last time friend-pick auto-resolution ran. This is DB-only (no API
# calls), so it's cheap enough to run frequently, but still throttled to
# avoid doing it on every single tight-live-window tick.
_LAST_FRIEND_RESOLVE: datetime | None = None

# v0.3.5: hard cap on how many matches get an actual fetch_odds() call in one
# tick, regardless of how many are in the tracked window. Priority order
# (see _match_priority) decides who gets the slots first, so a large tracked
# set can never starve a match that just went live.
MAX_MATCHES_PER_TICK = 60


def cadence_seconds(s2k: float) -> float:
    """Budget-fit cadence (2026-07 recalibration — see docs/DECISIONS.md D17).
    Original 60/15/3/1s cascade cost ~113 req/match lifecycle. At ESoccer's
    match-launch rate across multiple concurrent leagues, tracking "every
    match" at that cadence needs ~13,560 req/hr against a 3,600 req/hr
    BetsAPI cap (3.7x over budget) — verified by direct calculation, not
    estimate. This table costs ~34 req/match (~105 matches/hr sustainable)
    and still resolves the first-live jump, just at 2s instead of 1s
    granularity. s2k = seconds to kickoff (negative = after KO)."""
    if s2k > 600:
        return 9999.0  # single opening-line pull; no repeat until <10min out
    if s2k > 120:
        return 60.0
    if s2k > 0:
        return 10.0
    if s2k > -30:
        return 2.0
    return 120.0  # one closing/CLV pull ~2min after live, then stop


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _match_priority(m: Match, now: datetime, live_missing_first_live: set[int]) -> tuple[int, float]:
    """Lower tuple = higher priority. Tiers (spec, v0.3.5 Provider Execution Fix):
    0. live and missing its first-live snapshot -- the one thing the poller
       must never be late for.
    1. within +/-30s of kickoff.
    2. within 2 minutes of kickoff (either side).
    3. already tracked pre-match (we've polled it before; s2k > 120).
    4. distant upcoming (never polled yet).
    Ties within a tier break on how close to kickoff the match is."""
    s2k = (m.start_time - now).total_seconds()
    if m.id in live_missing_first_live:
        tier = 0
    elif abs(s2k) <= 30:
        tier = 1
    elif abs(s2k) <= 120:
        tier = 2
    elif m.id in _LAST_POLLED:
        tier = 3
    else:
        tier = 4
    return (tier, abs(s2k))


def process_snapshots(db, match: Match, incoming: list[dict]) -> dict:
    """Store snapshots + emit MarketEvents. Pure function of (prev state, tick) —
    unit-testable without a live provider."""
    now = _now()
    written = events = 0
    prev = {}
    existing_ticks = set()
    for sn in db.scalars(select(OddsSnapshot).where(OddsSnapshot.match_id == match.id)
                         .order_by(OddsSnapshot.collected_at)).all():
        prev[(sn.sportsbook, sn.market, sn.selection, sn.line)] = sn
        existing_ticks.add((sn.sportsbook, sn.market, sn.selection, sn.line,
                            sn.collected_at, sn.decimal_odds))
    seen_keys = set()
    had_live_before = any(p.phase == "live" for p in prev.values())

    for o in incoming:
        s2k = (match.start_time - o["collected_at"]).total_seconds()
        phase = "live" if s2k <= 0 else "pre_match"
        key = (o["sportsbook"], o["market"], o["selection"], o.get("line"))
        seen_keys.add(key)
        p = prev.get(key)
        tick_key = (o["sportsbook"], o["market"], o["selection"], o.get("line"),
                    o["collected_at"], o["decimal_odds"])
        if tick_key in existing_ticks:
            continue
        snap = OddsSnapshot(match_id=match.id, sportsbook=o["sportsbook"],
                            market=o["market"], selection=o["selection"],
                            line=o.get("line"), american_odds=o["american_odds"],
                            decimal_odds=o["decimal_odds"], implied_prob=o["implied_prob"],
                            collected_at=o["collected_at"], phase=phase,
                            seconds_to_kickoff=round(s2k, 1),
                            is_opening=p is None, is_closing=False,
                            data_source="betsapi", verification_status="api_verified")
        db.add(snap)
        existing_ticks.add(tick_key)
        written += 1

        def emit(etype, detail):
            nonlocal events
            db.add(MarketEvent(match_id=match.id, sportsbook=o["sportsbook"],
                               market=o["market"], selection=o["selection"],
                               event_type=etype, detail_json=json.dumps(detail), at=now))
            events += 1

        if p is None:
            emit("appeared", {"decimal": o["decimal_odds"]})
        else:
            move = round(o["decimal_odds"] - p.decimal_odds, 4)
            if abs(move) >= 0.10:
                emit("odds_change", {"from": p.decimal_odds, "to": o["decimal_odds"],
                                     "move": move, "phase": phase})
        if phase == "live" and not had_live_before:
            emit("live_start", {"first_live_decimal": o["decimal_odds"], "s2k": round(s2k, 1)})
            had_live_before = True
        # threshold cross vs any recommendation minimum
        for rec in db.scalars(select(Recommendation).where(
                Recommendation.match_id == match.id,
                Recommendation.min_american_odds.is_not(None))).all():
            was_ok = p is not None and p.american_odds >= rec.min_american_odds
            now_ok = o["american_odds"] >= rec.min_american_odds
            if was_ok and not now_ok:
                emit("threshold_cross", {"direction": "below_min",
                                         "min": rec.min_american_odds,
                                         "now": o["american_odds"], "rec_id": rec.id})

    # markets that existed before but not in this tick = disappeared
    for key, p in prev.items():
        if key not in seen_keys and incoming:
            db.add(MarketEvent(match_id=match.id, sportsbook=key[0], market=key[1],
                               selection=key[2], event_type="disappeared",
                               detail_json="{}", at=now))
            events += 1
    db.commit()
    return {"written": written, "events": events}


def ingest_ended_results(db, provider, tracked: list[str]) -> dict:
    """v0.3.5: pull BetsAPI's ended-results feed and upsert final scores into
    EXISTING tracked matches. Never creates a Match row from an ended event
    that wasn't already discovered via upcoming/inplay -- those are reported
    as unmatched, not silently inserted. upsert_match already only sets a
    field when the incoming value is not None, so this can never overwrite a
    real score with null (routers/data.py:upsert_match)."""
    ended = provider.fetch_results()
    scoped = [e for e in ended
             if any(t.lower() in (e.get("league") or "").lower() for t in tracked)] if tracked else ended
    matched = 0
    unmatched: list[str] = []
    scores_updated = 0
    for ev in scoped:
        existing = db.execute(select(Match).where(Match.ext_id == ev["ext_id"])).scalar_one_or_none()
        if existing is None:
            unmatched.append(ev["ext_id"])
            continue
        matched += 1
        had_score_before = existing.home_score is not None and existing.away_score is not None
        upsert_match(db, {**ev, "verification_status": "api_verified"})
        if not had_score_before and ev.get("home_score") is not None:
            scores_updated += 1
    db.commit()

    newly_scored = 0
    scoring_errors = 0
    try:
        from ..engines.prediction_lab import score_predictions
        result = score_predictions(db)
        newly_scored = result["scored"]
    except Exception:
        scoring_errors = 1

    return {
        "at": _now().isoformat(),
        "ended_events_fetched": len(ended),
        "in_tracked_leagues": len(scoped),
        "matched_to_existing_matches": matched,
        "scores_updated": scores_updated,
        "unmatched_ended_events": len(unmatched),
        "unmatched_ext_ids_sample": unmatched[:20],
        "predictions_newly_scored": newly_scored,
        "scoring_errors": scoring_errors,
    }


def performance_report(db) -> dict:
    """v0.3.5: poller/provider performance metrics for a validation session.
    Call-volume and empty-rate figures are computed from stored raw payloads
    (RawProviderResponse), not in-memory counters, so they're accurate no
    matter which process/instance made the call."""
    now = _now()
    one_min_ago = now - timedelta(minutes=1)
    one_hour_ago = now - timedelta(hours=1)

    odds_calls_last_minute = db.scalar(select(func.count(RawProviderResponse.id)).where(
        RawProviderResponse.endpoint == "/v2/event/odds",
        RawProviderResponse.at >= one_min_ago)) or 0

    calls_by_endpoint = dict(db.execute(
        select(RawProviderResponse.endpoint, func.count(RawProviderResponse.id))
        .where(RawProviderResponse.at >= one_hour_ago)
        .group_by(RawProviderResponse.endpoint)).all())

    from ..connectors.betsapi_provider import sportsbook_empty_stats
    book_stats = sportsbook_empty_stats(db)
    calls_by_sportsbook = {b: v["calls"] for b, v in book_stats.items()}
    empty_rate_by_sportsbook = {b: v["empty_rate"] for b, v in book_stats.items()}

    latencies = [v for (v,) in db.execute(
        select(PredictionReality.first_live_after_s)
        .where(PredictionReality.first_live_after_s.is_not(None))).all()]
    avg_latency = round(statistics.mean(latencies), 2) if latencies else None
    if len(latencies) >= 2:
        p95_latency = round(statistics.quantiles(latencies, n=20)[18], 2)
    elif latencies:
        p95_latency = latencies[0]
    else:
        p95_latency = None
    pct_within_15s = (round(100 * sum(1 for x in latencies if x <= 15) / len(latencies), 1)
                      if latencies else None)

    return {
        "loop_duration_s": STATUS.get("loop_duration_s"),
        "odds_calls_last_minute": odds_calls_last_minute,
        "active_tracked_matches": STATUS.get("active_tracked_matches"),
        "first_live_candidates": STATUS.get("first_live_candidates"),
        "avg_first_live_latency_s": avg_latency,
        "p95_first_live_latency_s": p95_latency,
        "pct_first_live_within_15s": pct_within_15s,
        "first_live_sample_size": len(latencies),
        "api_calls_last_hour_by_endpoint": calls_by_endpoint,
        "api_calls_by_sportsbook": calls_by_sportsbook,
        "empty_odds_rate_by_sportsbook": empty_rate_by_sportsbook,
        "validation_mode": STATUS.get("validation_mode"),
    }


async def poll_loop(provider_factory):
    """Background task. provider_factory(db) -> provider with fetch_odds(ext_id)."""
    STATUS.update(running=True, note="poller running")
    try:
        while True:
            tick_start = time.monotonic()
            db = SessionLocal()
            try:
                s = db.get(Settings, 1)
                if not s or not s.poller_enabled:
                    STATUS["note"] = "poller disabled in settings"
                    await asyncio.sleep(10)
                    continue
                provider = provider_factory(db)
                STATUS["provider"] = provider.name
                if not getattr(provider, "token", ""):
                    STATUS["note"] = "no provider key configured; idling"
                    await asyncio.sleep(30)
                    continue
                # Budget brake: BetsAPI quota is a fixed hourly window with no
                # mid-hour replenish (their docs: wait for next hour or buy a
                # volume package). Below 5% remaining, stop polling and hold
                # the rest as headroom for manual/alert-driven calls until reset.
                rem = provider.last_status.get("quota_remaining")
                lim = provider.last_status.get("quota_limit")
                if rem is not None and lim and rem < lim * 0.05:
                    STATUS["note"] = (f"quota brake: {rem}/{lim} left; paused until "
                                      f"{provider.last_status.get('quota_resets_at')}")
                    await asyncio.sleep(60)
                    continue
                now = _now()
                tracked = json.loads(s.tracked_leagues or "[]")
                if not tracked:
                    STATUS["note"] = ("poller enabled but tracked_leagues is empty in "
                                      "Settings -- polling nothing to protect API budget (D18)")
                    await asyncio.sleep(30)
                    continue

                validation_mode = bool(s.validation_mode_enabled)
                STATUS["validation_mode"] = validation_mode

                # D27: discovery step -- was missing entirely. Nothing else in
                # the codebase ever inserted upcoming (unplayed) matches with a
                # real ext_id; backfill.py only saves FINISHED history. Without
                # this, the poller's match-selection query below always
                # returned zero rows, no matter how long it ran. Throttled to
                # once/60s (matches don't appear faster than that) -- costs
                # ~60 req/hr, trivial against the ~394 req/hr odds-polling
                # estimate (D23) and the 3600/hr cap.
                global _LAST_DISCOVERY
                if _LAST_DISCOVERY is None or (now - _LAST_DISCOVERY).total_seconds() >= 60:
                    upcoming = provider.fetch_upcoming()
                    if hasattr(provider, "fetch_inplay"):
                        upcoming = upcoming + provider.fetch_inplay()
                    by_ext = {e.get("ext_id") or f"{e.get('start_time')}-{e.get('home_player')}-{e.get('away_player')}": e for e in upcoming}
                    upcoming = list(by_ext.values())
                    scoped = [e for e in upcoming
                              if any(t.lower() in (e.get("league") or "").lower() for t in tracked)]
                    new_count = 0
                    for ev in scoped:
                        _, created = upsert_match(db, ev)
                        new_count += created
                    db.commit()
                    _LAST_DISCOVERY = now
                    STATUS["last_discovery"] = now.isoformat()
                    STATUS["discovery_found"] = len(scoped)
                    STATUS["discovery_new"] = new_count

                window = [m for m in db.scalars(select(Match).where(
                    Match.home_score.is_(None),
                    Match.start_time > now - timedelta(minutes=15),
                    Match.start_time < now + timedelta(minutes=30),
                    Match.ext_id.is_not(None))).all()
                    if any(t.lower() in (m.league or "").lower() for t in tracked)]

                # v0.3.5 priority ordering: don't treat every tracked match
                # equally near kickoff. A match that's live and still missing
                # its first-live snapshot always goes first, then proximity
                # to kickoff, then already-tracked, then distant upcoming.
                # Computed over the FULL window, before any validation-mode
                # narrowing -- see the bug note below.
                all_ids = {m.id for m in window}
                live_with_first_live_all = {
                    mid for (mid,) in db.execute(
                        select(OddsSnapshot.match_id).distinct()
                        .where(OddsSnapshot.match_id.in_(all_ids), OddsSnapshot.phase == "live")
                    ).all()
                } if all_ids else set()
                live_missing_first_live_all = {
                    m.id for m in window
                    if (m.start_time - now).total_seconds() <= 0 and m.id not in live_with_first_live_all
                }

                # v0.3.5 First-Live Validation Mode: narrow to the N
                # highest-priority matches, so first-live latency can be
                # measured under controlled (not production) load.
                #
                # Bug found 2026-07-06 during a max_matches=2 validation run:
                # this used to rank candidates by raw abs(seconds_to_kickoff),
                # which treats "5 minutes past kickoff" as just as "far" as
                # "5 minutes until kickoff." A match that had just gone live
                # got evicted the moment ANY other match came within a couple
                # minutes of its own (future) kickoff -- so a live match was
                # starved of its first-live repoll almost every time, which
                # defeated the entire point of the mode. Ranking by the same
                # _match_priority tiers used for normal tracking (live +
                # missing first-live is always tier 0) fixes this: a live
                # match that still needs its first-live snapshot can never be
                # displaced by a merely-imminent one.
                if validation_mode:
                    cap = max(1, s.validation_max_matches or 5)
                    window = sorted(window, key=lambda m: _match_priority(m, now, live_missing_first_live_all))[:cap]

                window_ids = {m.id for m in window}
                live_missing_first_live = {mid for mid in live_missing_first_live_all if mid in window_ids}
                window = sorted(window, key=lambda m: _match_priority(m, now, live_missing_first_live))

                STATUS["active_tracked_matches"] = len(window)
                STATUS["first_live_candidates"] = len(live_missing_first_live)

                # Loop sleep must follow the tightest cadence actually in play.
                # A flat 10s sleep silently capped the live bucket (2s) at 10s —
                # exactly the first-live window the steam thesis depends on.
                if window:
                    tightest = min(cadence_seconds((m.start_time - now).total_seconds())
                                   for m in window)
                    min_wait = max(1.0, min(tightest, 30.0))
                else:
                    min_wait = 30.0
                polled_this_tick = 0
                books = json.loads(s.sportsbooks_tracked or "[]") or ["bet365"]
                for m in window:
                    if polled_this_tick >= MAX_MATCHES_PER_TICK:
                        break  # hard cap: lower-priority matches wait for the next tick
                    s2k = (m.start_time - now).total_seconds()
                    interval = cadence_seconds(s2k)
                    last = _LAST_POLLED.get(m.id)
                    due = last is None or (now - last).total_seconds() >= interval
                    if s2k > 600 and last is not None:
                        continue  # D17: one opening pull only, never repeat in this bucket
                    if not due:
                        continue
                    _LAST_POLLED[m.id] = now
                    polled_this_tick += 1
                    # v0.3.5: with the bet365-only default there's one call
                    # per match per tick; a book that's returned nothing for
                    # a while is still tried (BetsAPI could add coverage any
                    # day) but never blocks bet365's own call from happening.
                    for book in books:
                        odds = provider.fetch_odds(m.ext_id, source=book)
                        if odds:
                            r = process_snapshots(db, m, odds)
                            STATUS["snapshots_written"] += r["written"]
                            STATUS["events_written"] += r["events"]

                # v0.3.5: ended-results ingestion, throttled to once/45s so it
                # never competes with the odds-polling loop above for a slot
                # inside the same tick's time budget.
                global _LAST_RESULTS_INGEST
                results_due = (_LAST_RESULTS_INGEST is None
                              or (now - _LAST_RESULTS_INGEST).total_seconds() >= 45)
                if results_due:
                    try:
                        STATUS["result_ingestion"] = ingest_ended_results(db, provider, tracked)
                    except Exception as e:
                        STATUS["result_ingestion_error"] = str(e)
                    finally:
                        _LAST_RESULTS_INGEST = now

                # Prediction Lab: freeze due horizons, refresh reality, score.
                # Throttled to once/20s — reality capture scans every match with
                # odds and grows with the DB; running it inside every 2s live
                # tick would delay first-live snapshot capture, the one thing
                # the poller must never be late for. 20s keeps horizon freezes
                # inside the 75s tolerance window.
                global _LAST_LAB_CYCLE
                lab_due = (_LAST_LAB_CYCLE is None
                           or (now - _LAST_LAB_CYCLE).total_seconds() >= 20)
                if lab_due:
                    try:
                        from ..engines.prediction_lab import run_prediction_lab_cycle
                        lab_cycle = run_prediction_lab_cycle(db)
                        STATUS["prediction_lab"] = {
                            "frozen": lab_cycle["frozen"]["created"],
                            "reality_rows": lab_cycle["reality"]["reality_rows_touched"],
                            "scored": lab_cycle["scored"]["scored"],
                        }
                    except Exception as e:  # poller must keep collecting odds even if lab scoring fails
                        STATUS["prediction_lab_error"] = str(e)
                    finally:
                        _LAST_LAB_CYCLE = now

                # v0.3.6: friend-pick auto-resolution -- DB-only, no API
                # calls, throttled to once/30s. Never blocks odds polling.
                global _LAST_FRIEND_RESOLVE
                friend_due = (_LAST_FRIEND_RESOLVE is None
                             or (now - _LAST_FRIEND_RESOLVE).total_seconds() >= 30)
                if friend_due:
                    try:
                        from ..engines.friend_picks import auto_resolve_pending, score_all_resolved
                        STATUS["friend_pick_resolution"] = auto_resolve_pending(db)
                        STATUS["friend_pick_scoring"] = score_all_resolved(db)
                    except Exception as e:
                        STATUS["friend_pick_resolution_error"] = str(e)
                    finally:
                        _LAST_FRIEND_RESOLVE = now

                # prune matches that dropped out of the tracking window
                for mid in list(_LAST_POLLED):
                    if mid not in {m.id for m in window}:
                        _LAST_POLLED.pop(mid, None)
                STATUS["ticks"] += 1
                STATUS["last_tick"] = now.isoformat()
                STATUS["loop_duration_s"] = round(time.monotonic() - tick_start, 3)
                STATUS["note"] = f"tracking {len(window)} matches, polled {polled_this_tick} this tick"
                await asyncio.sleep(min_wait if window else 30)
            finally:
                db.close()
    except asyncio.CancelledError:
        STATUS.update(running=False, note="poller stopped")
        raise
