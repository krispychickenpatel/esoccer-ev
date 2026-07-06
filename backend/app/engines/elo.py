"""Elo rating engine for ESoccer players with an explicit draw model.

Win/draw/loss probabilities use the Davidson (1970) extension of Bradley-Terry:

    s_h = 10^(R_h/400),  s_a = 10^(R_a/400)
    denom = s_h + s_a + nu * sqrt(s_h * s_a)
    p_home = s_h / denom
    p_away = s_a / denom
    p_draw = nu * sqrt(s_h * s_a) / denom

nu controls draw frequency: at equal ratings p_draw = nu / (2 + nu).
Default nu=0.63 gives ~24% draws at equal strength — roughly right for
8-minute ESoccer Battle matches, but REFIT THIS on your real data
(nu = 2*d / (1-d) where d is observed equal-strength draw rate).

Rating updates use standard Elo with score 1 / 0.5 / 0 against the
expected score E = p_home + 0.5*p_draw, plus an optional margin-of-victory
multiplier (log of goal difference) so 5-0 moves ratings more than 1-0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

DEFAULT_ELO = 1500.0
DEFAULT_K = 32.0
DEFAULT_NU = 0.63


def win_draw_loss_probs(elo_home: float, elo_away: float, nu: float = DEFAULT_NU) -> tuple[float, float, float]:
    s_h = 10.0 ** (elo_home / 400.0)
    s_a = 10.0 ** (elo_away / 400.0)
    root = math.sqrt(s_h * s_a)
    denom = s_h + s_a + nu * root
    return s_h / denom, nu * root / denom, s_a / denom


def mov_multiplier(goal_diff: int) -> float:
    """Margin-of-victory scaling: 1.0 for draws/1-goal, grows with log of margin."""
    return 1.0 if abs(goal_diff) <= 1 else math.log(abs(goal_diff)) + 1.0


@dataclass
class EloEngine:
    k: float = DEFAULT_K
    nu: float = DEFAULT_NU
    ratings: dict[int, float] = field(default_factory=dict)
    played: dict[int, int] = field(default_factory=dict)

    def get(self, player_id: int) -> float:
        return self.ratings.get(player_id, DEFAULT_ELO)

    def probs(self, home_id: int, away_id: int) -> tuple[float, float, float]:
        return win_draw_loss_probs(self.get(home_id), self.get(away_id), self.nu)

    def confidence(self, home_id: int, away_id: int) -> float:
        """0-1: how much to trust this prediction. Pure sample-size heuristic —
        each player's contribution saturates at 25 matches played."""
        n_h = min(self.played.get(home_id, 0), 25) / 25.0
        n_a = min(self.played.get(away_id, 0), 25) / 25.0
        return round((n_h + n_a) / 2.0, 3)

    def update(self, home_id: int, away_id: int, home_score: int, away_score: int) -> tuple[float, float, float, float]:
        """Apply a finished match. Returns (home_before, home_after, away_before, away_after)."""
        r_h, r_a = self.get(home_id), self.get(away_id)
        p_h, p_d, _ = win_draw_loss_probs(r_h, r_a, self.nu)
        expected_home = p_h + 0.5 * p_d
        if home_score > away_score:
            actual = 1.0
        elif home_score < away_score:
            actual = 0.0
        else:
            actual = 0.5
        delta = self.k * mov_multiplier(home_score - away_score) * (actual - expected_home)
        self.ratings[home_id] = r_h + delta
        self.ratings[away_id] = r_a - delta
        self.played[home_id] = self.played.get(home_id, 0) + 1
        self.played[away_id] = self.played.get(away_id, 0) + 1
        return r_h, r_h + delta, r_a, r_a - delta
