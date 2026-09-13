import pytest

from parlaybot.trends import (
    TrendConfig,
    candidate_thresholds,
    current_streak,
    shrink,
    weighted_hit_rate,
)


def test_recency_weighting_favours_recent_games():
    """Same 5/10 record, but the hits are recent in one case and stale in the other."""
    hot = [1] * 5 + [0] * 5      # newest-first: hit the last 5
    cold = [0] * 5 + [1] * 5
    h_hits, h_total, _ = weighted_hit_rate(hot, 1, 0.93)
    c_hits, c_total, _ = weighted_hit_rate(cold, 1, 0.93)
    assert h_hits / h_total > c_hits / c_total


def test_raw_hit_rate_is_unweighted():
    _, _, raw = weighted_hit_rate([1] * 5 + [0] * 5, 1, 0.93)
    assert raw == pytest.approx(0.5)


def test_shrinkage_pulls_small_perfect_samples_down():
    """5-for-5 must not read as a certainty."""
    w_hits, w_total, _ = weighted_hit_rate([3] * 5, 1, 1.0)
    p = shrink(w_hits, w_total, prior_prob=0.6, prior_strength=5.0)
    assert 0.6 < p < 0.85


def test_shrinkage_matters_less_with_a_big_sample():
    small = shrink(*weighted_hit_rate([3] * 5, 1, 1.0)[:2], 0.6, 5.0)
    large = shrink(*weighted_hit_rate([3] * 40, 1, 1.0)[:2], 0.6, 5.0)
    assert large > small
    assert large < 1.0


def test_streak_counts_only_from_the_front():
    assert current_streak([2, 2, 2, 0, 2, 2], 1) == 3
    assert current_streak([0, 2, 2], 1) == 0


def test_thresholds_stop_at_the_median():
    vals = [0, 1, 1, 2, 2, 2, 3, 4, 5, 9]
    ts = candidate_thresholds(vals, step=1)
    assert max(ts) <= 3  # median of that list is 2, rounded up to 3
    assert min(ts) >= 1


def test_thresholds_respect_step_for_yardage():
    vals = [22, 35, 48, 51, 67, 80]
    ts = candidate_thresholds(vals, step=5)
    assert all(t % 5 == 0 for t in ts)


def test_config_defaults_are_conservative():
    cfg = TrendConfig()
    assert cfg.min_games >= 8
    assert 0.85 <= cfg.decay < 1.0
    assert cfg.prior_strength > 0


def test_the_season_prior_outweighs_a_hot_window():
    """Regression test for the -1010 vs -320 miss.

    A 61%-season hitter who went 14/15 must not price like a 90% certainty.
    At prior_strength 5 this produced -1010 where FanDuel had -320.
    """
    from parlaybot.trends import adjust, shrink, weighted_hit_rate
    from parlaybot.odds import estimate_price, american_to_prob

    vals = [1.0] * 14 + [0.0] + [1.0] * 66 + [0.0] * 50   # 80/131, 14/15 recent
    cfg = TrendConfig()
    w_h, w_t, _ = weighted_hit_rate(vals[:cfg.window], 1, cfg.decay)
    base = shrink(w_h, w_t, sum(vals) / len(vals), cfg.prior_strength)
    prob, _ = adjust(base, streak=12, is_home=False, short_rest=False,
                     volatility=0.0, cfg=cfg)
    price = estimate_price(prob, 0.06)
    assert -450 < price < -200, f"priced {price:.0f}, market had this near -320"
    assert prob < 0.80


def test_model_probability_is_capped():
    """Selecting the top of a wide scan makes the highest estimates the least
    trustworthy, so there is a hard ceiling on what can be printed."""
    from parlaybot.trends import adjust
    cfg = TrendConfig()
    prob, _ = adjust(0.999, streak=20, is_home=True, short_rest=False,
                     volatility=0.0, cfg=cfg)
    assert prob <= cfg.max_model_prob


def test_streak_bonus_is_small_because_it_double_counts():
    cfg = TrendConfig()
    assert cfg.max_trend_bonus <= 0.08, (
        "the streak IS the hot window, which the recency weighting already "
        "counts; a large bonus counts it twice"
    )
