"""Odds conversions, vig removal, EV and staking math.

All functions are pure — no DB access — so they are trivially testable and
reusable by the backtester, the alert engine, and the API layer.
"""
from __future__ import annotations


def american_to_decimal(american: int | float) -> float:
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be 0")
    if a > 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def decimal_to_american(decimal: float) -> int:
    if decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100)
    return round(-100.0 / (decimal - 1.0))


def implied_prob(decimal: float) -> float:
    """Implied probability WITH vig (raw 1/odds)."""
    if decimal <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    return 1.0 / decimal


def remove_vig(implied: list[float]) -> list[float]:
    """Multiplicative (proportional) de-vig across a full market.

    Pass the raw implied probabilities of *every* selection in the market
    (e.g. [home, draw, away]). Returns fair probabilities summing to 1.
    If the list is incomplete the result is wrong — caller must ensure the
    market is complete or skip de-vigging.
    """
    total = sum(implied)
    if total <= 0:
        raise ValueError("Implied probabilities must be positive")
    return [p / total for p in implied]


def overround(implied: list[float]) -> float:
    """Book margin: sum of raw implied probs minus 1. ~0.08 = 8% hold."""
    return sum(implied) - 1.0


def expected_value(model_prob: float, decimal_odds: float) -> float:
    """EV per 1 unit staked. 0.05 = +5%."""
    return model_prob * decimal_odds - 1.0


def kelly_fraction(model_prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction of bankroll for a binary outcome.

    f* = (p*b - q) / b  where b = decimal - 1, q = 1 - p.
    Returns 0 when edge is non-positive.
    """
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - model_prob
    f = (model_prob * b - q) / b
    return max(0.0, f)


def suggested_stakes(
    model_prob: float,
    decimal_odds: float,
    bankroll: float,
    unit_size: float,
    kelly_mult: float = 0.25,
    max_bet: float | None = None,
) -> dict:
    """Flat-unit and fractional-Kelly stake suggestions, capped at max_bet."""
    ev = expected_value(model_prob, decimal_odds)
    flat = unit_size if ev > 0 else 0.0
    kelly = kelly_fraction(model_prob, decimal_odds) * kelly_mult * bankroll
    if max_bet is not None:
        flat = min(flat, max_bet)
        kelly = min(kelly, max_bet)
    return {"ev": ev, "flat": round(flat, 2), "kelly": round(kelly, 2)}


def clv_pct(placed_decimal: float, closing_decimal: float) -> float:
    """Closing line value: how much better your price was than the close.

    Positive = you beat the close. Computed on raw prices; if you have the
    full closing market you can de-vig both sides first for a cleaner number,
    but raw CLV on the same book/market is a consistent directional signal.
    """
    return placed_decimal / closing_decimal - 1.0
