"""The core-band gate: only a strong trend buys a leg in at the extremes."""

from datetime import date, timedelta

import pytest

from parlaybot.models import GameLogEntry, PlayerSeason
from parlaybot.trends import TrendConfig, build_legs_for_player, has_strong_trend

MARKETS = {"hits": {"label": "Hits", "step": 1}}
BAND = (-1200, -400)
CORE = (-900, -550)


def player(pattern: list[int], name: str = "P") -> PlayerSeason:
    """`pattern` is newest-first hit counts."""
    today = date.today()
    logs = [
        GameLogEntry(game_date=today - timedelta(days=i + 1), opponent="OPP",
                     home=True, stats={"hits": float(v)}, minutes=4.0)
        for i, v in enumerate(pattern)
    ]
    return PlayerSeason(player_id=name, name=name, team="AAA", sport="MLB",
                        logs=logs)


def legs_for(pattern, core=CORE, cfg=None):
    return build_legs_for_player(
        player(pattern), markets=MARKETS, game_id="G1", game_label="A @ B",
        is_home=True, short_rest=False, cfg=cfg or TrendConfig(), hold=0.06,
        price_band=BAND, core_band=core,
    )


# -- the strong-trend test itself -------------------------------------------

def test_strong_trend_needs_a_perfect_recent_run():
    cfg = TrendConfig()
    assert has_strong_trend([1] * 15, 1, cfg)
    # One miss inside the last 8 disqualifies it.
    assert not has_strong_trend([1, 1, 1, 0, 1, 1, 1, 1] + [1] * 7, 1, cfg)


def test_strong_trend_also_needs_the_wider_window():
    """8/8 recently but cold before that is not a strong trend."""
    cfg = TrendConfig()
    pattern = [1] * 8 + [0] * 7      # 8/15 overall = 0.53
    assert not has_strong_trend(pattern, 1, cfg)
    pattern = [1] * 12 + [0] * 3     # 12/15 = 0.80, exactly the floor
    assert has_strong_trend(pattern, 1, cfg)


def test_strong_trend_needs_enough_games():
    cfg = TrendConfig()
    assert not has_strong_trend([1] * 5, 1, cfg)


# -- the gate in build_legs_for_player --------------------------------------

def test_gate_is_off_when_core_band_equals_price_band():
    """No core band configured means the old behaviour, unchanged."""
    ungated = legs_for([2, 1, 1, 2, 0, 1, 1, 2, 1, 1, 0, 1, 1, 2, 1], core=BAND)
    gated = legs_for([2, 1, 1, 2, 0, 1, 1, 2, 1, 1, 0, 1, 1, 2, 1], core=CORE)
    assert len(ungated) >= len(gated)


def test_every_surviving_leg_is_in_core_or_has_a_strong_trend():
    """The invariant the whole feature exists to guarantee."""
    patterns = [
        [1] * 15,                                     # perfect
        [2, 1, 1, 2, 0, 1, 1, 2, 1, 1, 0, 1, 1, 2, 1],  # patchy
        [1] * 8 + [0] * 7,                            # hot then cold
        [3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3],  # high volume
    ]
    cfg = TrendConfig()
    for pattern in patterns:
        for leg in legs_for(pattern, cfg=cfg):
            in_core = min(CORE) <= leg.est_price <= max(CORE)
            if not in_core:
                values = [g.stats["hits"] for g in player(pattern).logs][:cfg.window]
                assert has_strong_trend(values, leg.threshold, cfg), (
                    f"{leg.est_price:.0f} is outside the core band with no "
                    f"strong trend behind it"
                )


def test_gated_legs_are_labelled():
    for leg in legs_for([1] * 15):
        if not (min(CORE) <= leg.est_price <= max(CORE)):
            assert any("strong trend" in n for n in leg.notes)


def test_patchy_form_loses_its_extreme_legs():
    """A player with an ordinary record keeps only core-band legs."""
    patchy = [2, 1, 1, 2, 0, 1, 1, 2, 1, 1, 0, 1, 1, 2, 1]
    for leg in legs_for(patchy):
        assert min(CORE) <= leg.est_price <= max(CORE)


def test_gate_never_widens_the_hard_band():
    for leg in legs_for([1] * 15):
        assert min(BAND) <= leg.est_price <= max(BAND)
