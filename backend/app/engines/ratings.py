"""Rating pipeline: replays finished matches chronologically, maintains Elo,
writes rating_history (the anti-lookahead backbone), and computes form/H2H
stats on demand.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, Player, RatingHistory
from .elo import EloEngine




def effective_scores(m) -> tuple[int, int] | None:
    """Scores if present; else synthesize from winner (D5: winner-only seed
    matches, MOV multiplier collapses to 1.0). None if neither exists."""
    if m.home_score is not None and m.away_score is not None:
        return m.home_score, m.away_score
    if m.winner == "home":
        return 1, 0
    if m.winner == "away":
        return 0, 1
    if m.winner == "draw":
        return 0, 0
    return None

FINISHED = (Match.home_score.is_not(None)) | (Match.winner.is_not(None))

def rebuild_ratings(db: Session, k: float = 32.0, nu: float = 0.63) -> EloEngine:
    """Full deterministic rebuild from scratch. Idempotent: wipes rating_history
    and replays every finished match in start_time order. Cheap up to ~100k
    matches on SQLite; move to incremental updates after that."""
    db.query(RatingHistory).delete()
    engine = EloEngine(k=k, nu=nu)

    matches = db.scalars(
        select(Match)
        .where(FINISHED)
        .order_by(Match.start_time, Match.id)
    ).all()

    goals_for: dict[int, list[int]] = {}
    goals_against: dict[int, list[int]] = {}

    for m in matches:
        es = effective_scores(m)
        if es is None:
            continue
        hs, as_ = es
        hb, ha, ab, aa = engine.update(m.home_player_id, m.away_player_id, hs, as_)
        db.add(RatingHistory(player_id=m.home_player_id, match_id=m.id, elo_before=hb, elo_after=ha, ts=m.start_time))
        db.add(RatingHistory(player_id=m.away_player_id, match_id=m.id, elo_before=ab, elo_after=aa, ts=m.start_time))
        if m.home_score is not None:  # only real scores feed attack/defense
            goals_for.setdefault(m.home_player_id, []).append(m.home_score)
            goals_against.setdefault(m.home_player_id, []).append(m.away_score)
            goals_for.setdefault(m.away_player_id, []).append(m.away_score)
            goals_against.setdefault(m.away_player_id, []).append(m.home_score)

    for p in db.scalars(select(Player)).all():
        p.elo = round(engine.get(p.id), 1)
        p.matches_played = engine.played.get(p.id, 0)
        gf = goals_for.get(p.id, [])[-25:]
        ga = goals_against.get(p.id, [])[-25:]
        p.attack = round(sum(gf) / len(gf), 2) if gf else 0.0
        p.defense = round(sum(ga) / len(ga), 2) if ga else 0.0

    db.commit()
    return engine


def elo_as_of(db: Session, player_id: int, ts: datetime) -> float:
    """Player's Elo just before timestamp ts, from rating_history. 1500 if unrated."""
    row = db.execute(
        select(RatingHistory)
        .where(RatingHistory.player_id == player_id, RatingHistory.ts < ts)
        .order_by(RatingHistory.ts.desc(), RatingHistory.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row.elo_after if row else 1500.0


def matches_played_before(db: Session, player_id: int, ts: datetime) -> int:
    from sqlalchemy import func
    return db.scalar(
        select(func.count(RatingHistory.id))
        .where(RatingHistory.player_id == player_id, RatingHistory.ts < ts)
    ) or 0


def player_form(db: Session, player_id: int, last_n: int = 10, before: datetime | None = None) -> dict:
    q = (
        select(Match)
        .where(
            FINISHED,
            (Match.home_player_id == player_id) | (Match.away_player_id == player_id),
        )
        .order_by(Match.start_time.desc())
        .limit(last_n)
    )
    if before is not None:
        q = q.where(Match.start_time < before)
    rows = db.scalars(q).all()
    w = d = losses = gf = ga = 0
    for m in rows:
        es = effective_scores(m)
        if es is None:
            continue
        is_home = m.home_player_id == player_id
        mine, theirs = es if is_home else (es[1], es[0])
        gf += mine
        ga += theirs
        if mine > theirs:
            w += 1
        elif mine == theirs:
            d += 1
        else:
            losses += 1
    n = len(rows)
    return {
        "n": n, "wins": w, "draws": d, "losses": losses,
        "win_pct": round(w / n, 3) if n else None,
        "draw_pct": round(d / n, 3) if n else None,
        "avg_gf": round(gf / n, 2) if n else None,
        "avg_ga": round(ga / n, 2) if n else None,
        "ppm": round((3 * w + d) / n, 3) if n else 0.0,  # points per match
    }


def head_to_head(db: Session, a_id: int, b_id: int, before: datetime | None = None) -> dict:
    q = select(Match).where(
        FINISHED,
        ((Match.home_player_id == a_id) & (Match.away_player_id == b_id))
        | ((Match.home_player_id == b_id) & (Match.away_player_id == a_id)),
    )
    if before is not None:
        q = q.where(Match.start_time < before)
    rows = db.scalars(q).all()
    a_wins = b_wins = draws = 0
    for m in rows:
        es = effective_scores(m)
        if es is None:
            continue
        if es[0] == es[1]:
            draws += 1
        else:
            home_won = es[0] > es[1]
            a_is_home = m.home_player_id == a_id
            if home_won == a_is_home:
                a_wins += 1
            else:
                b_wins += 1
    return {"n": len(rows), "a_wins": a_wins, "b_wins": b_wins, "draws": draws}
