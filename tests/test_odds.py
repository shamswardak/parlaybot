import math

import pytest

from parlaybot.odds import (
    american_to_decimal,
    american_to_prob,
    apply_hold,
    breakeven_probability,
    decimal_to_american,
    estimate_price,
    expected_value,
    joint_probability,
    parlay_decimal,
    prob_to_american,
    remove_hold,
    target_leg_decimal,
)


def test_american_decimal_roundtrip():
    for a in (-1200, -650, -400, -110, 100, 150, 1500):
        assert decimal_to_american(american_to_decimal(a)) == pytest.approx(a, abs=1)


def test_known_conversions():
    assert american_to_decimal(-650) == pytest.approx(1.153846, rel=1e-5)
    assert american_to_decimal(1500) == pytest.approx(16.0)
    assert american_to_prob(-650) == pytest.approx(650 / 750, rel=1e-6)


def test_hold_roundtrip():
    p = 0.87
    assert remove_hold(apply_hold(p, 0.06), 0.06) == pytest.approx(p, rel=1e-9)


def test_estimate_price_is_shorter_than_fair():
    """Adding hold must make the price worse for the bettor, never better."""
    fair = prob_to_american(0.85)
    priced = estimate_price(0.85, 0.06)
    assert priced < fair  # more negative == shorter price


def test_target_leg_decimal_reproduces_target():
    target = 16.0
    for n in (15, 17, 20):
        d = target_leg_decimal(target, n)
        assert parlay_decimal([d] * n) == pytest.approx(target, rel=1e-9)


def test_twenty_legs_at_650_lands_near_target():
    """The user's stated shape: ~20 legs at -650 should be in +1500 territory."""
    d = american_to_decimal(-650)
    total = parlay_decimal([d] * 20)
    assert 15.0 < total < 20.0
    assert 1400 < decimal_to_american(total) < 1900


def test_expected_value_and_breakeven_agree():
    probs = [0.87] * 18
    decs = [american_to_decimal(-650)] * 18
    total = parlay_decimal(decs)
    be = breakeven_probability(total)
    ev = expected_value(probs, decs)
    # EV above 1 exactly when the model beats the break-even number.
    assert (ev > 1.0) == (joint_probability(probs) > be)


def test_joint_probability_shrinks_with_legs():
    assert joint_probability([0.9] * 20) < joint_probability([0.9] * 10)
    assert joint_probability([0.9] * 20) == pytest.approx(0.9 ** 20)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        american_to_decimal(0)
    with pytest.raises(ValueError):
        decimal_to_american(1.0)
