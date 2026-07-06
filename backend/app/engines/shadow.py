"""Shadow Model analytics, league/sportsbook intelligence, and Data Health.

The Shadow Model treats human/friend picks as a measurable prediction source —
a signal to be scored, never the truth (spec). Seed rows are always labeled.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..connectors.betsapi_provider import sportsbook_empty_stats
from ..models import (Bet, ExecutionLog, Match, OddsSnapshot, Pick, Player,
                      Recommendation)
from .identity import canonical_name
from .research import _stats, wilson


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latency_bucket(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    if sec <= 10:
        return "0-10s"
    if sec <= 30:
        return "10-30s"
    return "30s+"


def shadow_dashboard(db: Session, include_seed: bool = True) -> dict:
    recs = db.scalars(select(Recommendation).where(
        Recommendation.source_name != "model")).all()
    if not include_seed:
        recs = [r for r in recs if r.data_source != "manual_seed"]
    rec_ids = {r.id for r in recs}
    bets = [b for b in db.scalars(select(Bet).where(
        Bet.result.in_(("win", "loss")))).all() if b.recommendation_id in rec_ids]
    execs = [e for e in db.scalars(select(ExecutionLog)).all()
             if e.recommendation_id in rec_ids]

    overall = _stats(bets)
    clvs = [b.clv_pct for b in bets if b.clv_pct is not None]
    overall["avg_clv_pct"] = round(sum(clvs) / len(clvs), 2) if clvs else None

    def bucketize(keyf):
        g = defaultdict(list)
        for b in bets:
            g[keyf(b)].append(b)
        return {k: _stats(v) for k, v in g.items()}

    exec_by_id = {e.recommendation_id: e for e in execs}
    profit_by_latency = defaultdict(list)
    for b in bets:
        e = exec_by_id.get(b.recommendation_id)
        profit_by_latency[_latency_bucket(e.latency_seconds if e else None)].append(b)

    # timing: received -> kickoff
    lead_times = []
    for r in recs:
        if r.received_at and r.scheduled_start:
            lead_times.append((r.scheduled_start - r.received_at).total_seconds() / 60)
    statuses = defaultdict(int)
    for r in recs:
        statuses[r.status] += 1
    n_recs = len(recs)

    # friend vs system pick agreement (over picks linked to recs)
    picks = db.scalars(select(Pick).where(Pick.recommendation_id.is_not(None))).all()
    consensus_perf = defaultdict(list)
    for p in picks:
        if p.settled_result in ("win", "loss") and p.profit is not None:
            consensus_perf[p.consensus].append(p)
    consensus_stats = {}
    for k, v in consensus_perf.items():
        wins = sum(1 for p in v if p.settled_result == "win")
        staked = sum(p.suggested_stake or 0 for p in v)
        profit = sum(p.profit or 0 for p in v)
        consensus_stats[k] = {"n": len(v), "wins": wins,
                              "roi_pct": round(profit / staked * 100, 2) if staked else None,
                              "win_rate_ci95": list(wilson(wins, len(v)))}

    return {
        "n_recommendations": n_recs,
        "status_counts": dict(statuses),
        "pass_miss_rate": round((statuses.get("missed", 0) + statuses.get("expired", 0))
                                / n_recs, 3) if n_recs else None,
        "avg_lead_time_min": round(sum(lead_times) / len(lead_times), 1) if lead_times else None,
        "settled_bets": overall,
        "profit_by_player": bucketize(lambda b: canonical_name(b.selection) or "?"),
        "profit_by_league": bucketize(lambda b: b.league or "?"),
        "profit_by_market": bucketize(lambda b: b.market),
        "profit_by_odds_range": bucketize(
            lambda b: "<1.5" if b.decimal_odds < 1.5 else "1.5-2.0" if b.decimal_odds < 2.0
            else "2.0-2.5" if b.decimal_odds < 2.5 else "2.5+"),
        "profit_by_latency": {k: _stats(v) for k, v in profit_by_latency.items()},
        "consensus_performance": consensus_stats,
        "seed_included": include_seed,
        "warning": ("Shadow stats include SEED (manually reconstructed) rows — "
                    "directionally useful, not proof." if include_seed else None),
    }


SEED_SOURCES = ("manual_seed", "synthetic_demo", "seed")


def league_profiles(db: Session, include_seed: bool = True) -> list[dict]:
    """D28: now respects include_seed -- previously always mixed synthetic
    demo + real seed bets into every league/book's numbers with zero flag,
    which is how FanDuel showed 86.6% ROI (100% seed-derived, n=10) presented
    identically to pinnacle/bet365's largely-synthetic numbers."""
    matches = db.scalars(select(Match).where(Match.home_score.is_not(None))).all()
    if not include_seed:
        matches = [m for m in matches if m.source not in SEED_SOURCES]
    by = defaultdict(list)
    for m in matches:
        by[m.league or "?"].append(m)
    out = []
    for lg, ms in by.items():
        goals = [m.home_score + m.away_score for m in ms]
        mean = sum(goals) / len(goals)
        var = sum((g - mean) ** 2 for g in goals) / len(goals)
        draws = sum(1 for m in ms if m.winner == "draw") / len(ms)
        lb = [b for b in db.scalars(select(Bet).where(Bet.result.in_(("win", "loss")))).all()
              if lg.lower() in (b.league or "").lower()
              and (include_seed or b.data_source not in SEED_SOURCES)]
        st = _stats(lb)
        out.append({"league": lg, "matches": len(ms), "avg_goals": round(mean, 2),
                    "goal_variance": round(var, 2), "draw_rate": round(draws, 3),
                    "bet_roi_pct": st["roi_pct"], "bet_n": st["n"],
                    "seed_influenced": any(m.source in SEED_SOURCES for m in ms) or
                                      any(b.data_source in SEED_SOURCES for b in lb)})
    return sorted(out, key=lambda x: -x["matches"])


def sportsbook_profiles(db: Session, include_seed: bool = True) -> list[dict]:
    """D28: now respects include_seed -- see league_profiles."""
    books = defaultdict(lambda: {"snapshots": 0, "bets": []})
    for snap in db.scalars(select(OddsSnapshot)).all():
        books[snap.sportsbook]["snapshots"] += 1
    bet_rows = db.scalars(select(Bet).where(Bet.result.in_(("win", "loss")))).all()
    if not include_seed:
        bet_rows = [b for b in bet_rows if b.data_source not in SEED_SOURCES]
    for b in bet_rows:
        if b.sportsbook:
            books[b.sportsbook]["bets"].append(b)
    execs = db.scalars(select(ExecutionLog)).all()
    lat_by = defaultdict(list)
    rej = defaultdict(lambda: [0, 0])
    for e in execs:
        if e.latency_seconds is not None:
            lat_by[e.sportsbook].append(e.latency_seconds)
        rej[e.sportsbook][0 if e.status == "placed" else 1] += 1
    limits = defaultdict(list)
    for r in db.scalars(select(Recommendation).where(
            Recommendation.limit_seen.is_not(None))).all():
        limits[r.sportsbook].append(r.limit_seen)
    out = []
    for book, d in books.items():
        st = _stats(d["bets"])
        clvs = [b.clv_pct for b in d["bets"] if b.clv_pct is not None]
        lats = lat_by.get(book, [])
        acc, rj = rej.get(book, [0, 0])
        out.append({
            "sportsbook": book, "snapshots": d["snapshots"],
            "bets": st["n"], "roi_pct": st["roi_pct"],
            "avg_clv_pct": round(sum(clvs) / len(clvs), 2) if clvs else None,
            "avg_exec_latency_s": round(sum(lats) / len(lats), 1) if lats else None,
            "accepted": acc, "rejected": rj,
            "avg_limit_seen": round(sum(limits[book]) / len(limits[book]), 0) if limits.get(book) else None,
            "live_refresh_speed": None,  # needs poller data across books
        })
    return sorted(out, key=lambda x: -(x["bets"] or 0))


def data_health(db: Session) -> dict:
    now = _now()
    n_matches = db.scalar(select(func.count(Match.id)))
    n_snaps = db.scalar(select(func.count(OddsSnapshot.id)))
    n_recs = db.scalar(select(func.count(Recommendation.id)))
    n_execs = db.scalar(select(func.count(ExecutionLog.id)))
    n_players = db.scalar(select(func.count(Player.id)))
    latest_snap = db.scalar(select(func.max(OddsSnapshot.collected_at)))
    latest_match = db.scalar(select(func.max(Match.start_time)))
    missing_scores = db.scalar(select(func.count(Match.id)).where(
        Match.home_score.is_(None), Match.start_time < now, Match.winner.is_(None)))
    matches_no_odds = 0
    match_ids_with_odds = set(db.scalars(select(OddsSnapshot.match_id).distinct()))
    for mid in db.scalars(select(Match.id)):
        if mid not in match_ids_with_odds:
            matches_no_odds += 1
    # duplicate suspects: same players + same start_time count > 1
    dupes = db.execute(select(Match.home_player_id, Match.away_player_id, Match.start_time,
                              func.count(Match.id))
                       .group_by(Match.home_player_id, Match.away_player_id, Match.start_time)
                       .having(func.count(Match.id) > 1)).all()
    invalid_odds = db.scalar(select(func.count(OddsSnapshot.id)).where(
        (OddsSnapshot.decimal_odds <= 1.0) | (OddsSnapshot.implied_prob <= 0)
        | (OddsSnapshot.implied_prob >= 1)))
    seed_counts = {
        "matches": db.scalar(select(func.count(Match.id)).where(
            Match.source.in_(("manual_seed", "seed", "synthetic_demo")))),
        "bets": db.scalar(select(func.count(Bet.id)).where(
            Bet.data_source.in_(("manual_seed", "synthetic_demo", "seed")))),
        "recommendations": db.scalar(select(func.count(Recommendation.id)).where(
            Recommendation.data_source == "manual_seed")),
    }
    live_snaps = db.scalar(select(func.count(OddsSnapshot.id)).where(
        OddsSnapshot.phase == "live"))
    stale = bool(latest_snap and (now - latest_snap).days >= 2)
    book_stats = sportsbook_empty_stats(db)
    empty_book_warnings = [
        f"SPORTSBOOK '{book}' returned empty odds on {v['empty']}/{v['calls']} recent calls "
        f"({round(v['empty_rate'] * 100)}%) — likely no esoccer coverage from this source; "
        "consider removing it from Settings > Sportsbooks tracked"
        for book, v in book_stats.items() if v["calls"] >= 5 and v["empty_rate"] >= 0.9
    ]
    return {
        "totals": {"matches": n_matches, "odds_snapshots": n_snaps,
                   "recommendations": n_recs, "executions": n_execs, "players": n_players},
        "latest_data": {"odds": latest_snap.isoformat() if latest_snap else None,
                        "match": latest_match.isoformat() if latest_match else None},
        "issues": {"missing_scores": missing_scores, "matches_without_odds": matches_no_odds,
                   "duplicate_match_suspects": len(dupes), "invalid_odds_rows": invalid_odds},
        "seed_counts": seed_counts,
        "live_phase_snapshots": live_snaps,
        "sportsbook_empty_stats": book_stats,
        "warnings": [w for w in [
            "STALE DATA: newest odds snapshot is 2+ days old" if stale else None,
            "SAMPLE/SEED DATA present — metrics may mix seed and verified rows "
            "(toggle in Settings)" if any(seed_counts.values()) else None,
            "No live-phase odds snapshots — execution-timing analytics are empty "
            "until the poller or timestamped CSVs feed them" if not live_snaps else None,
            *empty_book_warnings,
        ] if w],
    }
