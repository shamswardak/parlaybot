import tempfile
from datetime import date

import pytest

from parlaybot.calibration import (
    Calibration,
    CalibrationConfig,
    bucket_of,
    fit,
    load,
    refresh,
    save,
)


def rows(n, prob, hit_rate, sport="MLB"):
    """n legs all predicted at `prob`, of which hit_rate actually hit."""
    hits = int(round(n * hit_rate))
    return [
        {"sport": sport, "model_prob": prob, "hit": i < hits}
        for i in range(n)
    ]


def test_buckets_are_monotonic_and_total():
    assert bucket_of(0.50) < bucket_of(0.80) < bucket_of(0.95)
    assert bucket_of(0.0) >= 0
    assert bucket_of(1.0) >= 0


def test_no_correction_below_the_sample_floor():
    cal = fit(rows(20, 0.88, 0.60), CalibrationConfig(min_samples=60))
    assert cal.shifts == {}, "a 20-leg sample must not move the model"
    assert cal.observed, "but it should still be recorded and reported"


def test_overconfidence_produces_a_negative_shift():
    """Model says 88%, reality is 70% -> future estimates must come down."""
    cal = fit(rows(500, 0.88, 0.70), CalibrationConfig(min_samples=60))
    assert cal.apply("MLB", 0.88) < 0.88


def test_underconfidence_produces_a_positive_shift():
    cal = fit(rows(500, 0.80, 0.92), CalibrationConfig(min_samples=60))
    assert cal.apply("MLB", 0.80) > 0.80


def test_well_calibrated_model_is_left_alone():
    cal = fit(rows(500, 0.86, 0.86), CalibrationConfig(min_samples=60))
    assert cal.apply("MLB", 0.86) == pytest.approx(0.86, abs=0.02)


def test_shrinkage_means_more_data_moves_it_further():
    cfg = CalibrationConfig(min_samples=60, prior_weight=200)
    small = fit(rows(80, 0.88, 0.70), cfg).apply("MLB", 0.88)
    large = fit(rows(2000, 0.88, 0.70), cfg).apply("MLB", 0.88)
    assert large < small, "sustained evidence should move the model more"


def test_shift_is_clamped():
    """Even a catastrophic stretch can't swing the model arbitrarily."""
    cfg = CalibrationConfig(min_samples=10, prior_weight=1.0, max_shift=0.45)
    cal = fit(rows(5000, 0.95, 0.05), cfg)
    assert all(abs(s) <= 0.45 + 1e-9 for s in cal.shifts.values())


def test_extreme_outcomes_do_not_blow_up():
    """A bucket where everything hit must not produce an infinite log-odds."""
    cal = fit(rows(300, 0.85, 1.0), CalibrationConfig(min_samples=60))
    for shift in cal.shifts.values():
        assert shift == shift  # not NaN
        assert abs(shift) != float("inf")


def test_applied_probability_stays_in_range():
    cal = fit(rows(1000, 0.95, 0.10), CalibrationConfig(min_samples=10))
    for p in (0.05, 0.5, 0.8, 0.999):
        out = cal.apply("MLB", p)
        assert 0.0 < out < 1.0


def test_sport_specific_beats_the_pooled_shift():
    data = rows(400, 0.88, 0.70, sport="MLB") + rows(400, 0.88, 0.95, sport="NFL")
    cal = fit(data, CalibrationConfig(min_samples=60))
    assert cal.apply("MLB", 0.88) < cal.apply("NFL", 0.88)


def test_unknown_sport_falls_back_to_pooled():
    cal = fit(rows(500, 0.88, 0.70), CalibrationConfig(min_samples=60))
    assert cal.apply("CRICKET", 0.88) < 0.88


def test_empty_calibration_is_a_no_op():
    cal = Calibration.empty()
    assert cal.apply("MLB", 0.87) == 0.87


def test_roundtrip_through_disk():
    with tempfile.TemporaryDirectory() as d:
        cal = fit(rows(500, 0.88, 0.70), CalibrationConfig(min_samples=60))
        save(d, cal)
        back = load(d)
        assert back.shifts == cal.shifts
        assert back.apply("MLB", 0.88) == cal.apply("MLB", 0.88)


def test_load_missing_file_is_safe():
    with tempfile.TemporaryDirectory() as d:
        assert load(d).apply("MLB", 0.9) == 0.9
