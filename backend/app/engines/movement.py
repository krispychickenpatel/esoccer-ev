"""Market Movement Engine (spec section).

All analysis derives from timestamped odds_snapshots — screenshots/manual rows are
backup evidence only. seconds_to_kickoff < 0 means AFTER kickoff (live phase).
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, OddsSnapshot

LIVE_BUCKETS = [
    ("pre_match", None, 0.0),        # before kickoff
    ("live_0_10s", 0.0, 10.0),       # 0-10s after live
    ("live_10_30s", 10.0, 30.0),
    ("live_30s_plus", 30.0, None),
]


def bucket_for(seconds_to_kickoff: float | None) -> str:
    """Map snapshot timing to execution-window bucket. s2k negative = after KO."""
    if seconds_to_kickoff is None or seconds_to_kickoff > 0:
        return "pre_match"
    after = -seconds_to_kickoff
    if after <= 10:
        return "live_0_10s"
    if after <= 30:
        return "live_10_30s"
    return "live_30s_plus"


def _s2k(match: Match, snap: OddsSnapshot) -> float | None:
    if snap.seconds_to_kickoff is not None:
        return snap.seconds_to_kickoff
    if match.start_time and snap.collected_at:
        return (match.start_time - snap.collected_at).total_seconds()
    return None


def match_timeline(db: Session, match_id: int, sportsbook: str | None = None,
                   market: str = "ML_3WAY", selection: str | None = None,
                   as_of=None) -> list[dict]:
    """Full odds timeline for a match: every update, timestamped, with s2k,
    movement direction and magnitude vs previous tick.

    as_of: optional leakage cutoff — only snapshots with collected_at <= as_of
    are included. Required when features feed a frozen prediction."""
    m = db.get(Match, match_id)
    if not m:
        return []
    q = select(OddsSnapshot).where(OddsSnapshot.match_id == match_id,
                                   OddsSnapshot.market == market)
    if as_of is not None:
        q = q.where(OddsSnapshot.collected_at <= as_of)
    if sportsbook:
        q = q.where(OddsSnapshot.sportsbook == sportsbook)
    if selection:
        q = q.where(OddsSnapshot.selection == selection)
    snaps = sorted(db.scalars(q).all(), key=lambda s: (s.selection, s.sportsbook, s.collected_at))
    out, prev = [], {}
    for s in snaps:
        key = (s.selection, s.sportsbook)
        p = prev.get(key)
        delta = round(s.decimal_odds - p.decimal_odds, 4) if p else 0.0
        s2k = _s2k(m, s)
        out.append({
            "t": s.collected_at.isoformat(), "sportsbook": s.sportsbook,
            "selection": s.selection, "american": s.american_odds,
            "decimal": s.decimal_odds, "s2k": s2k, "bucket": bucket_for(s2k),
            "move": delta, "direction": "shorten" if delta < 0 else "drift" if delta > 0 else "flat",
            "is_opening": s.is_opening, "is_closing": s.is_closing, "phase": s.phase,
        })
        prev[key] = s
    return out


def movement_metrics(db: Session, match_id: int, selection: str,
                     market: str = "ML_3WAY", sportsbook: str | None = None,
                     as_of=None) -> dict:
    """Opening, pre-live, first-live, first-live jump, volatility, closing."""
    tl = [t for t in match_timeline(db, match_id, sportsbook, market, selection, as_of=as_of)]
    if not tl:
        return {"n_snapshots": 0}
    pre = [t for t in tl if t["bucket"] == "pre_match"]
    live = [t for t in tl if t["bucket"] != "pre_match"]
    opening = next((t for t in tl if t["is_opening"]), tl[0])
    closing = next((t for t in reversed(tl) if t["is_closing"]), None)
    pre_live_last = pre[-1] if pre else None
    first_live = live[0] if live else None
    first_live_jump = (round(first_live["decimal"] - pre_live_last["decimal"], 4)
                       if first_live and pre_live_last else None)
    decs = [t["decimal"] for t in tl]
    volatility = round(statistics.pstdev(decs), 4) if len(decs) > 1 else 0.0
    return {
        "n_snapshots": len(tl),
        "opening_decimal": opening["decimal"],
        "pre_live_decimal": pre_live_last["decimal"] if pre_live_last else None,
        "first_live_decimal": first_live["decimal"] if first_live else None,
        "first_live_jump": first_live_jump,
        "closing_decimal": closing["decimal"] if closing else None,
        "total_move": round(tl[-1]["decimal"] - opening["decimal"], 4),
        "volatility": volatility,
        "live_ticks": len(live),
    }


def aggregate_first_live_moves(db: Session) -> dict:
    """Avg first-live movement by player / league / market; requires live-phase
    snapshots (poller or timestamped CSVs). Empty-safe."""
    matches = db.scalars(select(Match).where(Match.home_score.is_not(None))).all()
    by_player, by_league, by_market = defaultdict(list), defaultdict(list), defaultdict(list)
    move_vs_result = []  # (jump on home selection, home won?)
    for m in matches:
        for market in ("ML_3WAY", "SPREAD_2WAY"):
            mm = movement_metrics(db, m.id, "home", market)
            j = mm.get("first_live_jump")
            if j is None:
                continue
            by_player[m.home_player.name].append(j)
            by_league[m.league].append(j)
            by_market[market].append(j)
            if m.winner:
                move_vs_result.append((j, m.winner == "home"))
    def avg(d):
        return {k: {"avg_jump": round(sum(v) / len(v), 4), "n": len(v)}
                for k, v in d.items() if v}
    # does movement predict result? shorten (jump<0) should imply higher win rate
    shortened = [w for j, w in move_vs_result if j < 0]
    drifted = [w for j, w in move_vs_result if j > 0]
    predictive = {
        "n": len(move_vs_result),
        "win_rate_when_shortened": round(sum(shortened) / len(shortened), 3) if shortened else None,
        "win_rate_when_drifted": round(sum(drifted) / len(drifted), 3) if drifted else None,
    }
    return {"by_player": avg(by_player), "by_league": avg(by_league),
            "by_market": avg(by_market), "movement_predicts_result": predictive}


def movement_signal_for(db: Session, match_id: int, selection: str, as_of=None) -> dict:
    """Live/pre-live signal for the ensemble: negative jump (shortening) on our
    selection = market agrees with us = positive signal.

    Leakage guard: pass as_of=prediction_time when this feeds a frozen
    prediction — otherwise the match's own first-live jump (the very thing the
    steam model is scored against) can leak into the feature set."""
    mm = movement_metrics(db, match_id, selection, as_of=as_of)
    jump = mm.get("first_live_jump")
    total = mm.get("total_move") or 0.0
    if mm.get("n_snapshots", 0) < 2:
        return {"signal": 0.0, "quality": 0.0, "reason": "no odds timeline", **mm}
    basis = jump if jump is not None else total
    # squash decimal-odds move into [-1, 1]; 0.15 decimal move ≈ strong
    signal = max(-1.0, min(1.0, -basis / 0.15))
    quality = 0.8 if jump is not None else 0.4  # first-live data beats open-vs-latest
    reason = (f"first-live jump {jump:+.3f}" if jump is not None
              else f"total move {total:+.3f} (no live ticks)")
    return {"signal": round(signal, 3), "quality": quality, "reason": reason, **mm}
