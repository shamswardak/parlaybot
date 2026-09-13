"""Prior-season games inform the baseline; they do not pass as current form."""

from datetime import date, timedelta

import pytest

from parlaybot.models import GameLogEntry, PlayerSeason
from parlaybot.trends import (
    TrendConfig,
    build_legs_for_player,
    current_streak,
    has_strong_trend,
    weighted_hit_rate,
)

MARKETS = {"hits": {"label": "Hits", "step": 1}}


def player(values, stale_flags, name="P"):
    today = date.today()
    logs = [
        GameLogEntry(game_date=today - timedelta(days=i + 1), opponent="OPP",
                     home=True, stats={"hits": float(v)}, minutes=4.0,
                     stale=s)
        for i, (v, s) in enumerate(zip(values, stale_flags))
    ]
    return PlayerSeason(player_id=name, name=name, team="AAA", sport="NFL",
                        logs=logs)


# -- streaks ----------------------------------------------------------------

def test_streak_stops_at_the_season_boundary():
    """Six straight last December is not a live six-game streak."""
    values = [1] * 10
    stale = [False, False] + [True] * 8
    assert current_streak(values, 1, stale) == 2
    assert current_streak(values, 1) == 10  # without the flags, the old answer


def test_streak_is_zero_when_all_form_is_last_season():
    assert current_streak([1] * 10, 1, [True] * 10) == 0


# -- weighting --------------------------------------------------------------

def test_stale_games_carry_less_weight():
    fresh = weighted_hit_rate([1] * 10, 1, 0.93, [False] * 10, 0.5)
    stale = weighted_hit_rate([1] * 10, 1, 0.93, [True] * 10, 0.5)
    assert stale[1] < fresh[1], "stale sample should count for less"
    # The RATE is unchanged -- a perfect record is still perfect, just thinner.
    assert stale[0] / stale[1] == pytest.approx(fresh[0] / fresh[1])


def test_stale_weight_of_zero_discards_last_season():
    _, total, _ = weighted_hit_rate([1] * 8, 1, 1.0, [True] * 8, 0.0)
    assert total == 0.0


def test_thinner_sample_shrinks_harder_toward_the_prior():
    """The real consequence: last season's form gets pulled to the baseline."""
    from parlaybot.trends import shrink
    fresh_h, fresh_n, _ = weighted_hit_rate([1] * 12, 1, 0.93, [False] * 12, 0.5)
    stale_h, stale_n, _ = weighted_hit_rate([1] * 12, 1, 0.93, [True] * 12, 0.5)
    assert shrink(stale_h, stale_n, 0.5, 5.0) < shrink(fresh_h, fresh_n, 0.5, 5.0)


# -- the strong-trend exception --------------------------------------------

def test_strong_trend_cannot_be_bought_with_last_season():
    cfg = TrendConfig()
    assert has_strong_trend([1] * 15, 1, cfg, [False] * 15)
    assert not has_strong_trend([1] * 15, 1, cfg, [True] * 15)


def test_strong_trend_rejects_a_mixed_recent_run():
    """Even one prior-season game inside the last 8 disqualifies it."""
    cfg = TrendConfig()
    stale = [False] * 7 + [True] * 8
    assert not has_strong_trend([1] * 15, 1, cfg, stale)


# -- end to end -------------------------------------------------------------

# 12/15 with a live six-game run at the front. A flawless log prices off the
# end of the band and gets filtered either way, leaving nothing to compare.
RECORD = [1, 1, 1, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 1]


def _legs(stale_flags, values=RECORD, cfg=None, min_games=None):
    return build_legs_for_player(
        player(values, stale_flags), markets=MARKETS, game_id="G1",
        game_label="A @ B", is_home=True, short_rest=False,
        cfg=cfg or TrendConfig(), hold=0.06, price_band=(-1200, -400),
        core_band=(-900, -550), min_games=min_games,
    )


def test_prior_season_only_player_is_not_priced_at_all():
    """The default policy: no current-season sample, no legs. Sit the sport out."""
    assert _legs([True] * 15) == []


def test_current_season_player_is_priced_normally():
    legs = _legs([False] * 15)
    assert legs
    for leg in legs:
        assert not any("last season" in n for n in leg.notes)


def test_a_short_current_season_sample_is_rejected():
    """Three games into a season is not a trend, whatever the record."""
    values = [1, 1, 1] + RECORD[:12]
    stale = [False] * 3 + [True] * 12
    assert _legs(stale, values=values, min_games=4) == []


def test_the_sample_floor_is_per_sport():
    """The same player clears a 4-game floor and fails a 12-game one.

    11 current-season games at 9/11 -- chosen to price inside the core band, so
    this test measures the sample floor and not the price gate.
    """
    values = [1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1] + RECORD[:4]
    stale = [False] * 11 + [True] * 4
    assert _legs(stale, values=values, min_games=4) != []
    assert _legs(stale, values=values, min_games=12) == []


# -- the opt-in path, for anyone who wants last season at reduced weight ----

def test_opting_in_prices_prior_season_but_without_the_streak():
    """use_prior_season=True keeps the games but never the streak.

    Staleness does not move the raw hit rate -- 12/15 is 12/15 whenever it
    happened. What it removes is the run, which is the point: a streak that
    ended in January is not a streak.
    """
    cfg = TrendConfig(use_prior_season=True)
    fresh = {(l.stat_key, l.threshold): l for l in _legs([False] * 15, cfg=cfg)}
    stale = {(l.stat_key, l.threshold): l for l in _legs([True] * 15, cfg=cfg)}
    shared = set(fresh) & set(stale)
    assert shared, "expected comparable legs in both cases"
    for key in shared:
        assert fresh[key].streak > 0 and stale[key].streak == 0
        assert stale[key].model_prob < fresh[key].model_prob
        assert any("last season" in n for n in stale[key].notes)
