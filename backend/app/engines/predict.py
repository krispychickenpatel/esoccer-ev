"""Prediction engine v1 (Elo + Davidson) and the EV opportunity scanner.

Upgrade path (Phase 2): build a feature matrix from Prediction.features_json
plus results, fit sklearn LogisticRegression (multinomial) with walk-forward
splits, register as model='logreg_v1'. The scanner is model-agnostic — it
just reads the latest Prediction row per match.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert, Match, OddsSnapshot, Player, Prediction, Settings
from . import odds_math
from .elo import win_draw_loss_probs
from .ratings import head_to_head, player_form

SELECTION_INDEX = {"home": 0, "draw": 1, "away": 2}


def predict_match(db: Session, match: Match, nu: float = 0.63) -> Prediction:
    """Predict from CURRENT player Elo (for upcoming matches).
    Backtests must NOT use this — they use elo_as_of()."""
    home = db.get(Player, match.home_player_id)
    away = db.get(Player, match.away_player_id)
    p_h, p_d, p_a = win_draw_loss_probs(home.elo, away.elo, nu)

    n_h = min(home.matches_played, 25) / 25.0
    n_a = min(away.matches_played, 25) / 25.0
    confidence = round((n_h + n_a) / 2.0, 3)

    features = {
        "elo_home": home.elo, "elo_away": away.elo, "elo_diff": round(home.elo - away.elo, 1),
        "form_home": player_form(db, home.id, 10, before=match.start_time),
        "form_away": player_form(db, away.id, 10, before=match.start_time),
        "h2h": head_to_head(db, home.id, away.id, before=match.start_time),
        "attack_home": home.attack, "defense_home": home.defense,
        "attack_away": away.attack, "defense_away": away.defense,
    }
    pred = Prediction(
        match_id=match.id, model="elo_davidson_v1",
        p_home=round(p_h, 4), p_draw=round(p_d, 4), p_away=round(p_a, 4),
        fair_home=round(1 / p_h, 3), fair_draw=round(1 / p_d, 3), fair_away=round(1 / p_a, 3),
        confidence=confidence, features_json=json.dumps(features),
    )
    db.add(pred)
    db.commit()
    return pred


def latest_snapshot_market(db: Session, match_id: int, sportsbook: str, market: str) -> list[OddsSnapshot]:
    """Latest snapshot per selection for one book+market on a match."""
    rows = db.scalars(
        select(OddsSnapshot)
        .where(OddsSnapshot.match_id == match_id,
               OddsSnapshot.sportsbook == sportsbook,
               OddsSnapshot.market == market)
        .order_by(OddsSnapshot.collected_at.desc(), OddsSnapshot.id.desc())
    ).all()
    seen: dict[tuple, OddsSnapshot] = {}
    for r in rows:
        key = (r.selection, r.line)
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def scan_ev_opportunities(db: Session, create_alerts: bool = False) -> list[dict]:
    """Compare model probabilities to latest book prices on upcoming ML_3WAY
    markets. Returns opportunities with EV >= settings.min_ev_pct."""
    s = db.get(Settings, 1)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    upcoming = db.scalars(
        select(Match).where(Match.home_score.is_(None), Match.start_time > now)
    ).all()

    out: list[dict] = []
    for m in upcoming:
        pred = db.execute(
            select(Prediction).where(Prediction.match_id == m.id)
            .order_by(Prediction.created_at.desc(), Prediction.id.desc()).limit(1)
        ).scalar_one_or_none()
        if pred is None:
            pred = predict_match(db, m)
        model_p = [pred.p_home, pred.p_draw, pred.p_away]

        books = {r.sportsbook for r in db.scalars(
            select(OddsSnapshot).where(OddsSnapshot.match_id == m.id)).all()}
        for book in books:
            snaps = latest_snapshot_market(db, m.id, book, "ML_3WAY")
            by_sel = {r.selection: r for r in snaps}
            if not by_sel:
                continue
            complete = all(k in by_sel for k in ("home", "draw", "away"))
            fair_book = None
            if complete:
                fair_book = odds_math.remove_vig([by_sel[k].implied_prob for k in ("home", "draw", "away")])
            for sel, snap in by_sel.items():
                idx = SELECTION_INDEX.get(sel)
                if idx is None:
                    continue
                p = model_p[idx]
                ev = odds_math.expected_value(p, snap.decimal_odds)
                if ev * 100 < s.min_ev_pct:
                    continue
                stakes = odds_math.suggested_stakes(
                    p, snap.decimal_odds, s.starting_bankroll, s.unit_size,
                    s.kelly_fraction, s.max_bet_size)
                reason = (
                    f"model p={p:.3f} vs book implied={snap.implied_prob:.3f}"
                    + (f" (de-vig {fair_book[idx]:.3f})" if fair_book else " (market incomplete, no de-vig)")
                    + f"; elo_diff={json.loads(pred.features_json).get('elo_diff')}"
                    + f"; confidence={pred.confidence}"
                )
                opp = {
                    "match_id": m.id,
                    "match": f"{m.home_player.name} vs {m.away_player.name}",
                    "start_time": m.start_time.isoformat(),
                    "sportsbook": book, "market": "ML_3WAY", "selection": sel,
                    "line": snap.line,
                    "book_american": snap.american_odds, "book_decimal": snap.decimal_odds,
                    "model_prob": round(p, 4),
                    "fair_decimal": round(1 / p, 3),
                    "ev_pct": round(ev * 100, 2),
                    "stake_flat": stakes["flat"], "stake_kelly": stakes["kelly"],
                    "confidence": pred.confidence, "reason": reason,
                }
                out.append(opp)
                if create_alerts:
                    exists = db.execute(
                        select(Alert).where(Alert.match_id == m.id, Alert.selection == sel,
                                            Alert.sportsbook == book, Alert.status == "open")
                    ).scalar_one_or_none()
                    if not exists:
                        db.add(Alert(
                            match_id=m.id, market="ML_3WAY", selection=sel, line=snap.line,
                            sportsbook=book, book_american=snap.american_odds,
                            book_decimal=snap.decimal_odds, model_prob=round(p, 4),
                            fair_decimal=round(1 / p, 3), ev_pct=round(ev * 100, 2),
                            suggested_stake=stakes["kelly"] or stakes["flat"], reason=reason))
    if create_alerts:
        db.commit()
    out.sort(key=lambda o: -o["ev_pct"])
    return out
