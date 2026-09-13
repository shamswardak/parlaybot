"""Odds math: American <-> decimal <-> probability, vig modelling, parlay pricing.

No live odds feed is used. Book prices are *estimated* from a modelled true
probability plus a market-hold assumption, so every price this bot prints is a
target to verify in the sportsbook, not a quote.
"""

from __future__ import annotations

import math


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------

def american_to_decimal(american: float) -> float:
    if american == 0:
        raise ValueError("American odds of 0 are not valid")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(decimal: float) -> float:
    if decimal <= 1.0:
        raise ValueError(f"Decimal odds must exceed 1.0 (got {decimal})")
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100.0)
    return round(-100.0 / (decimal - 1.0))


def decimal_to_prob(decimal: float) -> float:
    return 1.0 / decimal


def prob_to_decimal(prob: float) -> float:
    if not 0.0 < prob < 1.0:
        raise ValueError(f"Probability must be strictly between 0 and 1 (got {prob})")
    return 1.0 / prob


def american_to_prob(american: float) -> float:
    return decimal_to_prob(american_to_decimal(american))


def prob_to_american(prob: float) -> float:
    return decimal_to_american(prob_to_decimal(prob))


def format_american(american: float) -> str:
    a = int(round(american))
    return f"+{a}" if a > 0 else str(a)


# --------------------------------------------------------------------------
# Vig / market modelling
# --------------------------------------------------------------------------

def apply_hold(true_prob: float, hold: float) -> float:
    """Convert a true probability into the implied probability a book would post.

    `hold` is the two-way overround (e.g. 0.06 == a 106% market). It is split
    proportionally across both sides, which is the standard multiplicative
    de-vig model run in reverse.

    A book never posts a price at 100% implied, so the result is capped.
    """
    implied = true_prob * (1.0 + hold)
    return min(implied, 0.985)


def remove_hold(implied_prob: float, hold: float) -> float:
    """Inverse of apply_hold: strip the assumed overround off a posted price."""
    return implied_prob / (1.0 + hold)


def estimate_price(true_prob: float, hold: float) -> float:
    """Estimated American price a book would offer for a leg of this probability."""
    return prob_to_american(apply_hold(true_prob, hold))


# --------------------------------------------------------------------------
# Parlay math
# --------------------------------------------------------------------------

def parlay_decimal(leg_decimals: list[float]) -> float:
    total = 1.0
    for d in leg_decimals:
        total *= d
    return total


def parlay_american(leg_decimals: list[float]) -> float:
    return decimal_to_american(parlay_decimal(leg_decimals))


def joint_probability(leg_probs: list[float]) -> float:
    """Independent joint probability. Same-game legs are positively correlated,
    so this understates the true hit rate for an SGP -- and the book shortens
    the payout to match. Flagged separately in the report."""
    p = 1.0
    for x in leg_probs:
        p *= x
    return p


def target_leg_decimal(target_payout_decimal: float, n_legs: int) -> float:
    """Per-leg decimal price needed for `n_legs` legs to multiply to the target."""
    return target_payout_decimal ** (1.0 / n_legs)


def expected_value(leg_probs: list[float], leg_decimals: list[float]) -> float:
    """EV per 1 unit staked. 1.0 == break even, <1.0 == long-run loss."""
    return joint_probability(leg_probs) * parlay_decimal(leg_decimals)


def breakeven_probability(total_decimal: float) -> float:
    return 1.0 / total_decimal


def kelly_fraction(prob: float, decimal: float) -> float:
    """Full-Kelly stake fraction. Negative means no bet."""
    b = decimal - 1.0
    if b <= 0:
        return 0.0
    return (prob * b - (1.0 - prob)) / b


def log_odds(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1.0 - p))


def inv_log_odds(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
