"""Unit tests for the statistical estimators."""

from __future__ import annotations

import math

import pytest

from adoptimizer.domain.statistics import (
    Z_95,
    clamp,
    estimate_rate,
    is_significantly_better,
    required_sample_size,
    safe_ratio,
    wilson_interval,
)


class TestEstimateRate:
    def test_no_data_returns_the_prior(self) -> None:
        assert estimate_rate(0, 0, prior_mean=0.025, prior_strength=500) == 0.025

    def test_abundant_data_converges_to_observed_rate(self) -> None:
        rate = estimate_rate(50_000, 1_000_000, prior_mean=0.025, prior_strength=500)
        assert rate == pytest.approx(0.05, rel=1e-3)

    def test_small_sample_is_shrunk_toward_the_prior(self) -> None:
        # 1 click out of 2 impressions would be a 50% CTR; shrinkage keeps it sane.
        rate = estimate_rate(1, 2, prior_mean=0.025, prior_strength=500)
        assert rate < 0.05
        assert rate > 0.025

    def test_negative_trials_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            estimate_rate(1, -1, prior_mean=0.02, prior_strength=10)


class TestSafeRatio:
    def test_zero_denominator_returns_default(self) -> None:
        assert safe_ratio(100, 0) == 0.0
        assert safe_ratio(100, 0, default=-1.0) == -1.0

    def test_normal_division(self) -> None:
        assert safe_ratio(1, 4) == 0.25


class TestWilsonInterval:
    def test_zero_trials_collapses_to_prior(self) -> None:
        interval = wilson_interval(0, 0, prior=0.02)
        assert (interval.lower, interval.upper, interval.point) == (0.02, 0.02, 0.02)
        assert interval.width == 0.0

    def test_bounds_are_within_unit_interval(self) -> None:
        interval = wilson_interval(300, 10_000)
        assert 0.0 <= interval.lower <= interval.point <= interval.upper <= 1.0

    def test_small_sample_has_a_wider_interval(self) -> None:
        narrow = wilson_interval(300, 10_000)
        wide = wilson_interval(3, 100)
        assert wide.width > narrow.width

    def test_same_rate_with_more_evidence_has_a_higher_lower_bound(self) -> None:
        # Both convert at 3%; only the sample size differs. The larger sample is
        # trustworthy, so its conservative lower bound must sit higher.
        thin = wilson_interval(3, 100)
        proven = wilson_interval(30, 1_000)
        assert proven.point == pytest.approx(thin.point)
        assert proven.lower > thin.lower

    def test_perfect_tiny_sample_stays_far_below_a_proven_rate(self) -> None:
        # 1-for-1 looks like a 100% CTR on raw rate; the interval must not let it
        # outrank a creative proven at 30% over 10k impressions.
        lucky = wilson_interval(1, 1)
        proven = wilson_interval(3_000, 10_000)
        assert proven.lower > lucky.lower

    def test_perfect_sample_upper_is_capped(self) -> None:
        assert wilson_interval(10, 10).upper == pytest.approx(1.0)


class TestSignificance:
    def test_large_clear_difference_is_significant(self) -> None:
        assert is_significantly_better(600, 10_000, 300, 10_000) is True

    def test_small_sample_difference_is_not_significant(self) -> None:
        assert is_significantly_better(3, 10, 1, 10) is False

    def test_equal_rates_are_not_significant(self) -> None:
        assert is_significantly_better(100, 1_000, 100, 1_000) is False

    def test_no_trials_is_not_significant(self) -> None:
        assert is_significantly_better(0, 0, 10, 100) is False

    def test_all_converted_pooled_edge_case(self) -> None:
        # pooled == 1.0 makes the standard error zero; guard must not divide.
        assert is_significantly_better(10, 10, 10, 10) is False


class TestRequiredSampleSize:
    def test_returns_positive_per_arm_size(self) -> None:
        size = required_sample_size(0.02, 0.10)
        assert size > 1_000

    def test_smaller_effect_needs_more_traffic(self) -> None:
        assert required_sample_size(0.02, 0.05) > required_sample_size(0.02, 0.20)

    @pytest.mark.parametrize("baseline", [0.0, -0.1])
    def test_invalid_baseline_returns_zero(self, baseline: float) -> None:
        assert required_sample_size(baseline, 0.1) == 0

    def test_effect_pushing_rate_above_one_returns_zero(self) -> None:
        assert required_sample_size(0.9, 0.5) == 0


class TestClamp:
    def test_clamps_both_ends(self) -> None:
        assert clamp(-5, 0, 10) == 0
        assert clamp(50, 0, 10) == 10
        assert clamp(5, 0, 10) == 5

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="low must not exceed high"):
            clamp(1, 10, 0)


def test_z95_is_the_two_sided_normal_quantile() -> None:
    assert pytest.approx(1.96, abs=1e-3) == Z_95
    assert math.isfinite(Z_95)
