"""Unit tests for performance scoring and pause decisions."""

from __future__ import annotations

import pytest

from adoptimizer.domain.scoring import (
    DEFAULT_MIN_IMPRESSIONS,
    rank_by_lower_bound,
    score_performance,
    should_pause,
)

STRONG = {
    "impressions": 20_000,
    "clicks": 1_000,
    "conversions": 100,
    "cost": 5_000.0,
    "revenue": 15_000.0,
}


class TestScorePerformance:
    def test_reference_performance_scores_near_the_top(self) -> None:
        # CTR 5%, CVR 10%, CPA 50, ROAS 3.0 with full evidence.
        breakdown = score_performance(**STRONG)
        assert breakdown.confidence == pytest.approx(1.0)
        assert breakdown.total == pytest.approx(95.0, abs=0.5)

    def test_components_sum_to_the_raw_score(self) -> None:
        breakdown = score_performance(**STRONG)
        raw = (
            breakdown.ctr_component
            + breakdown.cvr_component
            + breakdown.cpa_component
            + breakdown.roas_component
        )
        assert breakdown.total == pytest.approx(raw, abs=1e-6)

    def test_no_conversions_gets_a_neutral_cpa_component(self) -> None:
        breakdown = score_performance(
            impressions=20_000, clicks=1_000, conversions=0, cost=5_000.0, revenue=0.0
        )
        # Neutral midpoint rather than zero, so an unproven creative is not punished.
        assert breakdown.cpa_component == pytest.approx(10.0)
        assert breakdown.confidence < 1.0

    def test_thin_evidence_is_blended_toward_neutral(self) -> None:
        thin = score_performance(impressions=10, clicks=0, conversions=0, cost=0.0)
        assert thin.confidence == pytest.approx(0.25, abs=0.01)
        # Blended 75% toward the neutral 50, never a hard zero.
        assert thin.total > 30.0

    def test_zero_inputs_do_not_raise(self) -> None:
        breakdown = score_performance(impressions=0, clicks=0, conversions=0, cost=0.0)
        assert 0.0 <= breakdown.total <= 100.0

    def test_as_dict_is_rounded_and_json_safe(self) -> None:
        payload = score_performance(**STRONG).as_dict()
        assert set(payload) == {
            "ctr_component",
            "cvr_component",
            "cpa_component",
            "roas_component",
            "confidence",
            "total",
        }
        assert all(isinstance(value, float) for value in payload.values())

    def test_worse_performance_scores_lower(self) -> None:
        good = score_performance(**STRONG)
        poor = score_performance(
            impressions=20_000, clicks=100, conversions=1, cost=5_000.0, revenue=100.0
        )
        assert poor.total < good.total


class TestShouldPause:
    def test_impression_gate_blocks_a_pause(self) -> None:
        pause, reason = should_pause(impressions=100, clicks=0, conversions=0, cost=50.0)
        assert pause is False
        assert str(DEFAULT_MIN_IMPRESSIONS) in reason

    def test_low_confidence_blocks_a_pause(self) -> None:
        pause, reason = should_pause(impressions=600, clicks=1, conversions=0, cost=10.0)
        assert pause is False
        assert "Confidence" in reason

    def test_proven_underperformer_is_paused(self) -> None:
        pause, reason = should_pause(
            impressions=20_000, clicks=100, conversions=1, cost=5_000.0, revenue=100.0
        )
        assert pause is True
        assert "below" in reason

    def test_strong_performer_is_never_paused(self) -> None:
        pause, reason = should_pause(**STRONG)
        assert pause is False
        assert "at or above" in reason

    def test_threshold_is_configurable(self) -> None:
        kwargs = {"impressions": 20_000, "clicks": 100, "conversions": 1, "cost": 5_000.0}
        assert should_pause(**kwargs, threshold=5.0)[0] is False
        assert should_pause(**kwargs, threshold=90.0)[0] is True


class TestRankByLowerBound:
    def test_more_evidence_outranks_a_lucky_small_sample(self) -> None:
        ranked = rank_by_lower_bound([("thin", 3, 100), ("proven", 300, 10_000)])
        assert [key for key, _ in ranked] == ["proven", "thin"]

    def test_same_rate_ranks_by_volume(self) -> None:
        ranked = rank_by_lower_bound([("a", 3, 100), ("b", 30, 1_000), ("c", 300, 10_000)])
        assert [key for key, _ in ranked] == ["c", "b", "a"]

    def test_ties_break_deterministically_on_key(self) -> None:
        ranked = rank_by_lower_bound([("z", 10, 100), ("a", 10, 100)])
        assert [key for key, _ in ranked] == ["a", "z"]

    def test_empty_input(self) -> None:
        assert rank_by_lower_bound([]) == []

    def test_lower_bounds_are_descending(self) -> None:
        ranked = rank_by_lower_bound([("a", 1, 10), ("b", 50, 100), ("c", 500, 1_000)])
        values = [value for _, value in ranked]
        assert values == sorted(values, reverse=True)
