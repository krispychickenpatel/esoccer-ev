"""Ensemble Decision System (spec: advanced prediction improvements #1).

Each signal model returns: pick side, probability estimate, confidence, reason,
data_quality. combine_signals() merges them with hand-set weights (D15) — every
number visible, nothing hidden.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Match, Recommendation
from . import ratings as R
from .elo import EloEngine, win_draw_loss_probs
from .movement import movement_signal_for

# D15: hand-set weights until there is data to fit on. Sum need not be 1 —
# combine_signals normalizes by (weight * quality).
WEIGHTS = {
    "elo": 0.30,
    "form": 0.15,
    "h2h": 0.10,
    "movement": 0.20,
    "shadow": 0.15,
    "league": 0.10,
}


@dataclass
class Signal:
    name: str
    pick: str          # home/away/draw/none
    p_home: float      # this signal's home win probability (3-way normalized)
    p_draw: float
    p_away: float
    confidence: float  # 0..1 self-assessed
    quality: float     # 0..1 data quality
    reason: str


def _davidson(eng: EloEngine, rh: float, ra: float) -> tuple[float, float, float]:
    return win_draw_loss_probs(rh, ra, nu=eng.nu)


def elo_signal(db: Session, m: Match, eng: EloEngine) -> Signal:
    rh = R.elo_as_of(db, m.home_player_id, m.start_time)
    ra = R.elo_as_of(db, m.away_player_id, m.start_time)
    ph, pd, pa = _davidson(eng, rh, ra)
    nh = R.matches_played_before(db, m.home_player_id, m.start_time)
    na = R.matches_played_before(db, m.away_player_id, m.start_time)
    quality = min(1.0, min(nh, na) / 25.0)
    pick = "home" if ph > pa else "away"
    return Signal("elo", pick, ph, pd, pa, abs(ph - pa), quality,
                  f"Elo {rh:.0f} vs {ra:.0f} ({nh}/{na} matches)")


def form_signal(db: Session, m: Match, eng: EloEngine) -> Signal:
    """Recent-form model: last-10 points-per-match differential mapped onto a
    small Elo adjustment (±60) over the base ratings."""
    fh = R.player_form(db, m.home_player_id, last_n=10, before=m.start_time)
    fa = R.player_form(db, m.away_player_id, last_n=10, before=m.start_time)
    if fh["n"] == 0 or fa["n"] == 0:
        return Signal("form", "none", 1/3, 1/3, 1/3, 0.0, 0.0, "insufficient form history")
    adj = (fh["ppm"] - fa["ppm"]) / 3.0 * 60.0   # ppm in [0,3] -> ±60 Elo
    rh = R.elo_as_of(db, m.home_player_id, m.start_time) + adj
    ra = R.elo_as_of(db, m.away_player_id, m.start_time) - adj
    ph, pd, pa = _davidson(eng, rh, ra)
    quality = min(1.0, min(fh["n"], fa["n"]) / 10.0)
    pick = "home" if ph > pa else "away"
    return Signal("form", pick, ph, pd, pa, abs(ph - pa), quality,
                  f"L10 ppm {fh['ppm']:.2f} vs {fa['ppm']:.2f}")


def h2h_signal(db: Session, m: Match) -> Signal:
    h = R.head_to_head(db, m.home_player_id, m.away_player_id, before=m.start_time)
    n = h["n"]
    if n == 0:
        return Signal("h2h", "none", 1/3, 1/3, 1/3, 0.0, 0.0, "no head-to-head history")
    # Laplace-smoothed empirical rates (a = home side of THIS match)
    ph = (h["a_wins"] + 1) / (n + 3)
    pd = (h["draws"] + 1) / (n + 3)
    pa = (h["b_wins"] + 1) / (n + 3)
    quality = min(1.0, n / 10.0)
    pick = "home" if ph > pa else "away"
    return Signal("h2h", pick, ph, pd, pa, abs(ph - pa), quality,
                  f"H2H {h['a_wins']}-{h['draws']}-{h['b_wins']} (n={n})")


def movement_sig(db: Session, m: Match, as_of=None) -> Signal:
    mv = movement_signal_for(db, m.id, "home", as_of=as_of)
    s, q = mv["signal"], mv["quality"]
    if q == 0.0:
        return Signal("movement", "none", 1/3, 1/3, 1/3, 0.0, 0.0, mv["reason"])
    # map signal [-1,1] to prob tilt around neutral 0.375/0.25/0.375
    tilt = 0.12 * s
    ph, pd, pa = 0.375 + tilt, 0.25, 0.375 - tilt
    pick = "home" if s > 0 else "away" if s < 0 else "none"
    return Signal("movement", pick, ph, pd, pa, abs(s), q, mv["reason"])


def shadow_signal(db: Session, m: Match) -> Signal:
    """Friend/human recommendation for this match, if any (Shadow Model as a
    prediction source). Confidence scales with the source's settled ROI later;
    at low sample it contributes direction, not certainty."""
    rec = db.scalar(select(Recommendation).where(
        Recommendation.match_id == m.id,
        Recommendation.source_name != "model"))
    if not rec or not rec.recommended_selection:
        return Signal("shadow", "none", 1/3, 1/3, 1/3, 0.0, 0.0, "no external pick")
    from .identity import canonical_name
    canon = canonical_name(rec.recommended_selection)
    side = "home" if canon == m.home_player.name else "away" if canon == m.away_player.name else "none"
    if side == "none":
        return Signal("shadow", "none", 1/3, 1/3, 1/3, 0.0, 0.2,
                      f"pick '{rec.recommended_selection}' didn't resolve to either side")
    p = {"high": 0.62, "medium": 0.55, "low": 0.48}.get(rec.confidence_label, 0.55)
    ph, pa = (p, 1 - p - 0.20) if side == "home" else (1 - p - 0.20, p)
    quality = 0.5 if rec.verification_status == "seed_partial" else 0.8
    return Signal("shadow", side, ph, 0.20, pa, p - 0.5 + 0.12, quality,
                  f"{rec.source_name} pick: {rec.recommended_selection} ({rec.confidence_label})")


def league_signal(db: Session, m: Match) -> Signal:
    """League strength/variance context: only adjusts draw mass, no side lean.
    Leakage guard: only matches that kicked off before this one count."""
    rows = db.scalars(select(Match).where(Match.league == m.league,
                                          Match.home_score.is_not(None),
                                          Match.start_time < m.start_time).limit(500)).all()
    scored = [r for r in rows if r.home_score is not None]
    if len(scored) < 20:
        return Signal("league", "none", 1/3, 1/3, 1/3, 0.0, 0.0, "league sample <20")
    draws = sum(1 for r in scored if r.winner == "draw") / len(scored)
    pd = draws
    ph = pa = (1 - pd) / 2
    return Signal("league", "none", ph, pd, pa, 0.1, min(1.0, len(scored) / 200),
                  f"league draw rate {draws:.0%} over {len(scored)} matches")


def all_signals(db: Session, m: Match, nu: float = 0.63, as_of=None) -> list[Signal]:
    """as_of: leakage cutoff for time-sensitive signals (movement). Elo/form/h2h
    are already bounded by m.start_time; pass as_of=prediction_time whenever
    the ensemble feeds a frozen prediction."""
    eng = EloEngine(nu=nu)
    return [elo_signal(db, m, eng), form_signal(db, m, eng), h2h_signal(db, m),
            movement_sig(db, m, as_of=as_of), shadow_signal(db, m), league_signal(db, m)]


def combine_signals(signals: list[Signal]) -> dict:
    """Transparent weighted mixture. Each signal's vote weight =
    WEIGHTS[name] * quality. Probabilities blended then renormalized."""
    tw = ph = pd = pa = 0.0
    for s in signals:
        w = WEIGHTS.get(s.name, 0.0) * s.quality
        if w <= 0:
            continue
        tw += w
        ph += w * s.p_home
        pd += w * s.p_draw
        pa += w * s.p_away
    if tw == 0:
        ph = pd = pa = 1 / 3
    else:
        ph, pd, pa = ph / tw, pd / tw, pa / tw
    z = ph + pd + pa
    ph, pd, pa = ph / z, pd / z, pa / z
    picks = [s.pick for s in signals if s.pick in ("home", "away")]
    agree = max(picks.count("home"), picks.count("away")) if picks else 0
    disagreement = 1 - (agree / len(picks)) if picks else 1.0
    return {"p_home": round(ph, 4), "p_draw": round(pd, 4), "p_away": round(pa, 4),
            "total_weight": round(tw, 3), "disagreement": round(disagreement, 3),
            "signals": [asdict(s) for s in signals]}
