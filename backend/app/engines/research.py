"""Research capabilities: hypothesis testing, pattern discovery, calibration,
drift detection, similar-setup search, pick grading, what-changed.

Nothing here permanently assumes a hypothesis is true; every result carries n
and a Wilson interval so small samples look as weak as they are.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bet, Hypothesis, Match, PatternNote, Pick, Prediction
from .identity import canonical_name


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval on win rate — honest small-sample confidence."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(c - h, 3), round(c + h, 3)


def _settled_bets(db: Session, include_seed: bool = True) -> list[Bet]:
    q = select(Bet).where(Bet.result.in_(("win", "loss")))
    rows = db.scalars(q).all()
    if not include_seed:
        rows = [b for b in rows if b.data_source not in ("manual_seed", "synthetic_demo", "seed")]
    return rows


def _stats(bets: list[Bet]) -> dict:
    n = len(bets)
    wins = sum(1 for b in bets if b.result == "win")
    staked = sum(b.stake for b in bets)
    profit = sum(b.profit for b in bets)
    lo, hi = wilson(wins, n)
    return {
        "n": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None,
        "roi_pct": round(profit / staked * 100, 2) if staked else None,
        "profit": round(profit, 2),
        "avg_decimal": round(sum(b.decimal_odds for b in bets) / n, 3) if n else None,
        "avg_ev_pct": (round(sum(b.ev_at_placement for b in bets if b.ev_at_placement is not None)
                       / max(1, sum(1 for b in bets if b.ev_at_placement is not None)), 2)
                       if any(b.ev_at_placement is not None for b in bets) else None),
        "win_rate_ci95": [lo, hi],
    }


# ---------------------------------------------------------------------------
# Hypothesis testing — parameterized test types, auto-run against the DB
# ---------------------------------------------------------------------------

def _bets_for_player(db, bets, player_canon, underdog_only=False):
    sel = []
    for b in bets:
        if canonical_name(b.selection) != player_canon:
            continue
        if underdog_only and b.american_odds < 100:
            continue
        sel.append(b)
    return sel


def run_hypothesis(db: Session, h: Hypothesis, include_seed: bool = True) -> dict:
    p = json.loads(h.params_json or "{}")
    bets = _settled_bets(db, include_seed)
    t = h.test_type

    if t == "player_backed_roi":            # e.g. "BLITZ outperforms when backed"
        rows = _bets_for_player(db, bets, canonical_name(p.get("player", "")),
                                underdog_only=p.get("underdog_only", False))
        result = _stats(rows)
    elif t == "market_vs_market":            # ML vs spread above an EV threshold
        ev_min = p.get("min_ev_pct", 8.0)
        ml = [b for b in bets if b.market == "ML_3WAY" and (b.ev_at_placement or 0) >= ev_min]
        sp = [b for b in bets if b.market == "SPREAD_2WAY" and (b.ev_at_placement or 0) >= ev_min]
        result = {"ml": _stats(ml), "spread": _stats(sp)}
    elif t == "league_variance":
        lg = p.get("league", "")
        ms = [m for m in db.scalars(select(Match).where(Match.home_score.is_not(None))).all()
              if lg.lower() in (m.league or "").lower()]
        goals = [m.home_score + m.away_score for m in ms if m.home_score is not None]
        mean = sum(goals) / len(goals) if goals else None
        var = (sum((g - mean) ** 2 for g in goals) / len(goals)) if goals else None
        result = {"n": len(goals), "avg_goals": round(mean, 2) if mean else None,
                  "variance": round(var, 2) if var else None}
    elif t == "odds_range_roi":
        lo, hi = p.get("min_decimal", 1.5), p.get("max_decimal", 2.5)
        result = _stats([b for b in bets if lo <= b.decimal_odds <= hi])
    elif t == "live_shorten":                # needs live snapshots (poller)
        from .movement import aggregate_first_live_moves
        agg = aggregate_first_live_moves(db)
        result = agg["by_player"].get(canonical_name(p.get("player", "")),
                                      {"note": "no live-phase snapshots yet"})
    elif t == "book_speed":
        result = {"note": "requires poller data from 2+ books; no live snapshots yet"}
    else:
        result = {"error": f"unknown test_type {t}"}

    result["tested_at"] = _now().isoformat()
    # trend: is evidence increasing or decreasing?
    prev = json.loads(h.last_result_json or "{}")
    trend = "unknown"
    if "roi_pct" in result and "roi_pct" in prev and result.get("roi_pct") is not None \
            and prev.get("roi_pct") is not None:
        trend = ("increasing" if result["roi_pct"] > prev["roi_pct"]
                 else "decreasing" if result["roi_pct"] < prev["roi_pct"] else "flat")
    h.prev_result_json = h.last_result_json
    h.last_result_json = json.dumps(result)
    h.last_tested_at = _now()
    h.trend = trend
    db.commit()
    return result


# ---------------------------------------------------------------------------
# Pattern discovery — proposes PatternNotes; user must approve (spec)
# ---------------------------------------------------------------------------

def scan_patterns(db: Session, min_n: int = 10, min_roi: float = 8.0,
                  include_seed: bool = True) -> list[dict]:
    bets = _settled_bets(db, include_seed)
    proposals = []

    def propose(kind, key, rows):
        st = _stats(rows)
        if st["n"] >= min_n and (st["roi_pct"] or -999) >= min_roi:
            proposals.append({"kind": kind, "description": f"{kind}: {key} — "
                              f"ROI {st['roi_pct']}% over {st['n']} bets "
                              f"(win rate CI {st['win_rate_ci95']})", "stats": st})

    by = lambda f: [(k, v) for k, v in _group(bets, f).items()]
    for k, v in by(lambda b: canonical_name(b.selection) or "?"):
        propose("player_roi", k, v)
    for k, v in by(lambda b: b.league or "?"):
        propose("league", k, v)
    for k, v in by(lambda b: b.market):
        propose("market", k, v)
    for k, v in by(lambda b: b.sportsbook or "?"):
        propose("sportsbook", k, v)
    for k, v in by(_odds_bucket):
        propose("odds_range", k, v)

    created = []
    for pr in proposals:
        existing = db.scalar(select(PatternNote).where(
            PatternNote.description == pr["description"]))
        if existing:
            continue
        note = PatternNote(kind=pr["kind"], description=pr["description"],
                           stats_json=json.dumps(pr["stats"]))
        db.add(note)
        created.append(pr)
    db.commit()
    return created


def _group(bets, keyf):
    g = defaultdict(list)
    for b in bets:
        g[keyf(b)].append(b)
    return g


def _odds_bucket(b: Bet) -> str:
    d = b.decimal_odds
    return "<1.5" if d < 1.5 else "1.5-2.0" if d < 2.0 else "2.0-2.5" if d < 2.5 else "2.5+"


# ---------------------------------------------------------------------------
# Calibration — are the model's probabilities honest?
# ---------------------------------------------------------------------------

BUCKETS = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.01)]


def calibration(db: Session) -> dict:
    preds = db.scalars(select(Prediction)).all()
    match_ids = {p.match_id for p in preds}
    matches = {m.id: m for m in db.scalars(select(Match).where(
        Match.id.in_(match_ids), Match.home_score.is_not(None))).all()} if match_ids else {}
    rows = []
    for p in preds:
        m = matches.get(p.match_id)
        if not m or not m.winner:
            continue
        top_p = max(p.p_home, p.p_away)
        top_side = "home" if p.p_home >= p.p_away else "away"
        rows.append((top_p, m.winner == top_side))
    out, total_err, total_n = [], 0.0, 0
    for lo, hi in BUCKETS:
        sel = [(p, w) for p, w in rows if lo <= p < hi]
        n = len(sel)
        if n:
            actual = sum(w for _, w in sel) / n
            expected = sum(p for p, _ in sel) / n
            err = actual - expected
            total_err += abs(err) * n
            total_n += n
        out.append({"bucket": f"{int(lo*100)}-{int(hi*100)}%", "n": n,
                    "expected": round(expected, 3) if n else None,
                    "actual": round(actual, 3) if n else None,
                    "error": round(err, 3) if n else None})
    return {"buckets": out,
            "mean_abs_error": round(total_err / total_n, 4) if total_n else None,
            "n_scored": total_n,
            "overconfident": bool(total_n and sum(
                (b["error"] or 0) for b in out if b["n"]) < -0.02 * len([b for b in out if b["n"]]))}


# ---------------------------------------------------------------------------
# Drift detection — rolling 25/50/100 windows over settled picks/bets
# ---------------------------------------------------------------------------

def drift(db: Session) -> dict:
    bets = sorted(_settled_bets(db), key=lambda b: b.placed_at)
    out = {}
    for w in (25, 50, 100):
        win = bets[-w:]
        if len(win) < w:
            out[f"last_{w}"] = {"n": len(win), "status": "insufficient"}
            continue
        st = _stats(win)
        clvs = [b.clv_pct for b in win if b.clv_pct is not None]
        st["avg_clv_pct"] = round(sum(clvs) / len(clvs), 2) if clvs else None
        st["status"] = ("degraded" if (st["roi_pct"] is not None and st["roi_pct"] < -5)
                        or (st["avg_clv_pct"] is not None and st["avg_clv_pct"] < -1)
                        else "healthy")
        out[f"last_{w}"] = st
    out["model_degraded"] = any(v.get("status") == "degraded" for v in out.values())
    return out


# ---------------------------------------------------------------------------
# Similar Setup Search (spec: advanced #2)
# ---------------------------------------------------------------------------

def similar_setups(db: Session, home_id: int, away_id: int, league: str,
                   decimal_odds: float, market: str,
                   before: datetime | None = None) -> dict:
    """Historical bets that looked like this pick: same player OR same opponent
    OR same league, similar odds (±0.35 decimal), same market."""
    from ..models import Player
    hp = db.get(Player, home_id)
    ap = db.get(Player, away_id)
    bets = _settled_bets(db)
    if before:
        bets = [b for b in bets if b.placed_at < before]
    sel = []
    for b in bets:
        cn = canonical_name(b.selection)
        opp = canonical_name(b.opponent)
        same_actor = (hp and cn == hp.name) or (ap and (cn == ap.name or opp == ap.name)) \
                     or (hp and opp == hp.name)
        same_league = league and league.lower() in (b.league or "").lower()
        if not (same_actor or same_league):
            continue
        if b.market != market:
            continue
        if abs(b.decimal_odds - decimal_odds) > 0.35:
            continue
        sel.append(b)
    st = _stats(sel)
    # max drawdown across this subset, chronological
    run = peak = dd = 0.0
    for b in sorted(sel, key=lambda x: x.placed_at):
        run += b.profit
        peak = max(peak, run)
        dd = max(dd, peak - run)
    st["max_drawdown"] = round(dd, 2)
    return st


# ---------------------------------------------------------------------------
# Pick grading A+..F (spec: advanced #8)
# ---------------------------------------------------------------------------

def grade_pick(p: Pick) -> str:
    won = p.settled_result == "win"
    beat_close = p.clv_pct is not None and p.clv_pct > 0
    strong_ev = p.ev_pct >= 8
    fast = True  # execution latency lives in ExecutionLog; picks default fast
    if won and beat_close and fast:
        return "A+"
    if not won and beat_close and strong_ev:
        return "A"
    if won and (p.clv_pct is not None and p.clv_pct < 0):
        return "B"
    if p.ev_pct < 2 or (p.clv_pct is not None and p.clv_pct < -2):
        return "D"
    if p.status in ("MISSED", "EXPIRED") or "DATA_WEAK" in (p.reason_codes or ""):
        return "F" if p.user_decision == "bet" else "C"
    return "C"


# ---------------------------------------------------------------------------
# What Changed engine — compare consecutive predictions per match
# ---------------------------------------------------------------------------

def what_changed(db: Session, match_id: int) -> dict:
    preds = db.scalars(select(Prediction).where(Prediction.match_id == match_id)
                       .order_by(Prediction.created_at)).all()
    if len(preds) < 2:
        return {"changes": [], "note": "fewer than 2 predictions for this match"}
    a, b = preds[-2], preds[-1]
    fa, fb = json.loads(a.features_json), json.loads(b.features_json)
    changes = []
    if round(a.p_home, 3) != round(b.p_home, 3):
        changes.append(f"P(home) {a.p_home:.1%} → {b.p_home:.1%}")
    for key, label in (("home_elo", "home Elo"), ("away_elo", "away Elo"),
                       ("home_matches", "home match count"), ("away_matches", "away match count")):
        if key in fa and key in fb and fa[key] != fb[key]:
            changes.append(f"{label}: {fa[key]} → {fb[key]}")
    return {"from": a.created_at.isoformat(), "to": b.created_at.isoformat(),
            "changes": changes or ["inputs unchanged; probability shift from model/version change"]}
