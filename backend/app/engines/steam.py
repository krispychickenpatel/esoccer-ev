"""Pre-kickoff Steam Predictor.

Purpose: before kickoff, estimate whether a side's price will shorten on the
first live tick. This is intentionally separate from outcome prediction.

It does NOT claim a winning side. It answers: "will this entry price still be
available once the match goes live?"
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, OddsSnapshot, Settings
from .odds_math import decimal_to_american

MODEL_VERSION = "steam_predictor_v1"
FEATURE_SET_VERSION = "steam_fs1"
SEED_SOURCES = {"manual_seed", "synthetic_demo", "seed"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class SteamMove:
    match_id: int
    league: str
    sportsbook: str
    market: str
    selection: str
    player_id: int | None
    pre_decimal: float
    first_live_decimal: float
    first_live_after_s: float

    @property
    def decimal_move(self) -> float:
        return self.first_live_decimal - self.pre_decimal

    @property
    def shortened(self) -> bool:
        return self.decimal_move < 0

    @property
    def cents(self) -> float:
        # decimal odds 2.20 -> 1.95 = 25 cents of price improvement.
        return round(self.decimal_move * 100, 1)


def _live_pairs(db: Session, before: datetime | None = None) -> list[SteamMove]:
    """Build historical pre-live -> first-live movements.

    Leakage guard: when called from a future/pending match, only include matches
    with start_time strictly before the prediction timestamp.
    """
    q = select(Match).where(Match.home_score.is_not(None))
    if before is not None:
        q = q.where(Match.start_time < before)
    matches = db.scalars(q).all()
    out: list[SteamMove] = []
    for m in matches:
        if m.source in SEED_SOURCES:
            continue
        snaps = db.scalars(select(OddsSnapshot).where(OddsSnapshot.match_id == m.id)
                           .order_by(OddsSnapshot.collected_at)).all()
        grouped: dict[tuple, list[OddsSnapshot]] = {}
        for s in snaps:
            if s.data_source in SEED_SOURCES:
                continue
            grouped.setdefault((s.sportsbook, s.market, s.selection, s.line), []).append(s)
        for (book, market, selection, _line), rows in grouped.items():
            pre = [r for r in rows if (r.seconds_to_kickoff if r.seconds_to_kickoff is not None else (m.start_time - r.collected_at).total_seconds()) > 0]
            live = [r for r in rows if (r.seconds_to_kickoff if r.seconds_to_kickoff is not None else (m.start_time - r.collected_at).total_seconds()) <= 0]
            if not pre or not live:
                continue
            last_pre = pre[-1]
            first_live = live[0]
            after_s = abs(first_live.seconds_to_kickoff if first_live.seconds_to_kickoff is not None else (m.start_time - first_live.collected_at).total_seconds())
            if after_s > 45:
                # First live tick too late to measure the execution edge.
                continue
            player_id = None
            if selection == "home":
                player_id = m.home_player_id
            elif selection == "away":
                player_id = m.away_player_id
            out.append(SteamMove(
                match_id=m.id, league=m.league, sportsbook=book, market=market,
                selection=selection, player_id=player_id,
                pre_decimal=last_pre.decimal_odds,
                first_live_decimal=first_live.decimal_odds,
                first_live_after_s=after_s,
            ))
    return out


def _summarize(rows: Iterable[SteamMove]) -> dict:
    rows = list(rows)
    if not rows:
        return {"n": 0, "steam_probability": 0.5, "avg_move_cents": 0.0,
                "median_first_live_after_s": None, "quality": 0.0}
    wins = sum(1 for r in rows if r.shortened)
    # Bayesian shrinkage to 50/50 so tiny samples do not scream certainty.
    p = (wins + 4) / (len(rows) + 8)
    cents = [r.cents for r in rows]
    return {
        "n": len(rows),
        "shortened": wins,
        "steam_probability": round(p, 3),
        "avg_move_cents": round(sum(cents) / len(cents), 1),
        "median_move_cents": round(statistics.median(cents), 1),
        "median_first_live_after_s": round(statistics.median([r.first_live_after_s for r in rows]), 1),
        "quality": round(_clamp(len(rows) / 30), 3),
    }


def steam_prediction_for_snapshot(db: Session, match: Match, snap: OddsSnapshot,
                                  settings: Settings | None = None,
                                  as_of: datetime | None = None) -> dict:
    """Predict first-live movement for the current market/selection snapshot.

    Leakage guard: history is bounded by min(kickoff, as_of). Callers that
    freeze a prediction with a historical prediction_time (Prediction Lab
    backfills, replays) MUST pass as_of=prediction_time, otherwise pairs that
    only became known after the prediction moment would leak into features
    (violates feature_timestamp <= prediction_timestamp).
    """
    before = min(match.start_time, as_of or _now())
    history = _live_pairs(db, before=before)
    if snap.selection == "home":
        pid = match.home_player_id
    elif snap.selection == "away":
        pid = match.away_player_id
    else:
        pid = None

    player_rows = [r for r in history if r.player_id == pid and r.market == snap.market and r.sportsbook == snap.sportsbook]
    league_rows = [r for r in history if r.league == match.league and r.market == snap.market and r.sportsbook == snap.sportsbook]
    market_rows = [r for r in history if r.market == snap.market and r.sportsbook == snap.sportsbook]
    global_rows = [r for r in history if r.market == snap.market]

    buckets = {
        "player_book_market": _summarize(player_rows),
        "league_book_market": _summarize(league_rows),
        "book_market": _summarize(market_rows),
        "global_market": _summarize(global_rows),
    }

    weighted = []
    for name, weight in (("player_book_market", 0.45), ("league_book_market", 0.30),
                         ("book_market", 0.15), ("global_market", 0.10)):
        b = buckets[name]
        if b["n"]:
            weighted.append((weight * max(0.15, b["quality"]), b))
    if weighted:
        denom = sum(w for w, _ in weighted)
        steam_p = sum(w * b["steam_probability"] for w, b in weighted) / denom
        move_cents = sum(w * b["avg_move_cents"] for w, b in weighted) / denom
        first_live_after = [b["median_first_live_after_s"] for _, b in weighted if b["median_first_live_after_s"] is not None]
        median_after = statistics.median(first_live_after) if first_live_after else None
        quality = _clamp(sum(w for w, _ in weighted))
    else:
        steam_p, move_cents, median_after, quality = 0.5, 0.0, None, 0.0

    # Use only shortening magnitude for first-live prediction. If history says
    # drift, predicted first-live equals current and decision should lean PASS/WAIT.
    expected_shorten_decimal = max(0.0, -move_cents / 100)
    predicted_live_decimal = max(1.01, snap.decimal_odds - expected_shorten_decimal)
    predicted_live_american = decimal_to_american(predicted_live_decimal)

    # Max entry = midpoint between current and expected live if steam is likely.
    # Example: +120 (2.20) -> -105 (~1.95) yields midpoint ~2.08 / +108.
    if steam_p >= 0.55 and expected_shorten_decimal > 0:
        max_entry_decimal = max(1.01, predicted_live_decimal + expected_shorten_decimal * 0.5)
    else:
        max_entry_decimal = snap.decimal_odds
    max_entry_american = decimal_to_american(max_entry_decimal)

    exec_cap = settings.exec_window_seconds if settings else 30
    if median_after is None:
        window_to = min(exec_cap, 12)
    else:
        window_to = min(exec_cap, max(4, int(round(median_after + 6))))

    status = "unknown"
    if quality == 0:
        status = "no_live_history"
    elif steam_p >= 0.62 and expected_shorten_decimal >= 0.05:
        status = "steam_likely"
    elif steam_p <= 0.45 and move_cents >= 0:
        status = "drift_likely"
    else:
        status = "mixed"

    reason_codes: list[str] = []
    if quality < 0.25:
        reason_codes.append("STEAM_DATA_WEAK")
    if status == "steam_likely":
        reason_codes.append("PRE_KICK_STEAM")
    elif status == "drift_likely":
        reason_codes.append("STEAM_AGAINST")

    return {
        "model_version": MODEL_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "current_american": snap.american_odds,
        "current_decimal": round(snap.decimal_odds, 3),
        "predicted_first_live_american": predicted_live_american,
        "predicted_first_live_decimal": round(predicted_live_decimal, 3),
        "steam_probability": round(steam_p, 3),
        "expected_line_movement_cents": round(move_cents, 1),
        "maximum_entry_price": max_entry_american,
        "maximum_entry_decimal": round(max_entry_decimal, 3),
        "execution_window": f"0-{window_to}s after live",
        "historical_sample": sum(b["n"] for b in buckets.values()),
        "quality": round(quality, 3),
        "status": status,
        "reason_codes": reason_codes,
        "buckets": buckets,
    }


def steam_report(db: Session) -> dict:
    rows = _live_pairs(db)
    by_key: dict[str, list[SteamMove]] = {}
    for r in rows:
        by_key.setdefault(f"{r.sportsbook} · {r.market}", []).append(r)
        by_key.setdefault(f"{r.league} · {r.sportsbook} · {r.market}", []).append(r)
    ranked = []
    for key, bucket in by_key.items():
        s = _summarize(bucket)
        if s["n"]:
            ranked.append({"segment": key, **s})
    ranked.sort(key=lambda x: (x["quality"], abs(x["steam_probability"] - 0.5)), reverse=True)
    return {
        "model_version": MODEL_VERSION,
        "total_first_live_pairs": len(rows),
        "summary": _summarize(rows),
        "segments": ranked[:50],
        "warning": None if rows else "No verified pre-live + first-live odds pairs yet. Steam prediction will stay WAIT/unknown until live odds capture is proven.",
    }
