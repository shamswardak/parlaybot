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
