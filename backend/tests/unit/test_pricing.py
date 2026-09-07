"""Unit tests for bid and eCPM pricing."""

from __future__ import annotations

import pytest

from adoptimizer.domain.pricing import (
    MIN_BID,
    ecpm,
    max_cpc,
    recommend_bid,
    roas_multiplier,
    volume_multiplier,
)


class TestEcpm:
    def test_formula(self) -> None:
        # eCPM = pCTR * pCVR * targetCPA * 1000
        assert ecpm(0.03, 0.05, 100.0) == pytest.approx(150.0)

    def test_zero_predictions_give_zero_value(self) -> None:
        assert ecpm(0.0, 0.05, 100.0) == 0.0

    def test_negative_inputs_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            ecpm(-0.01, 0.05, 100.0)


class TestMaxCpc:
    def test_is_cvr_times_target_cpa(self) -> None:
        assert max_cpc(0.05, 100.0) == pytest.approx(5.0)

    def test_zero_cvr_means_no_click_value(self) -> None:
        assert max_cpc(0.0, 100.0) == 0.0


class TestRoasMultiplier:
    @pytest.mark.parametrize(
        ("observed", "expected"),
        [(5.0, 1.3), (2.5, 1.1), (2.0, 1.0), (1.7, 1.0), (1.4, 0.8), (0.5, 0.6), (0.0, 0.6)],
    )
    def test_bands(self, observed: float, expected: float) -> None:
        multiplier, reasoning = roas_multiplier(observed, 2.0)
        assert multiplier == pytest.approx(expected)
        assert reasoning

    def test_unset_target_holds_the_bid_flat(self) -> None:
        multiplier, reasoning = roas_multiplier(9.0, 0.0)
        assert multiplier == 1.0
        assert "not set" in reasoning

    def test_no_revenue_explains_the_cut(self) -> None:
        _, reasoning = roas_multiplier(0.0, 2.0)
        assert "No observed revenue" in reasoning


class TestVolumeMultiplier:
    def test_learning_phase_complete_is_neutral(self) -> None:
        assert volume_multiplier(10_000, 10_000) == 1.0
        assert volume_multiplier(99_999, 10_000) == 1.0

    def test_mid_learning_phase_is_eased(self) -> None:
        assert volume_multiplier(5_000, 10_000) == pytest.approx(0.925)

    def test_no_impressions_uses_the_floor(self) -> None:
        assert volume_multiplier(0, 10_000) == pytest.approx(0.85)


class TestRecommendBid:
    def test_strategy_multiplier_is_applied_exactly_once(self) -> None:
        """Regression: the demo multiplied by ROAS then divided by it again.

        With the cap disabled the recommended bid must scale linearly with the
        strategy multiplier, otherwise the adjustment has cancelled itself out.
        """
        kwargs = {
            "campaign_id": "c1",
            "predicted_ctr": 0.03,
            "predicted_cvr": 0.05,
            "impressions": 50_000,
            "target_cpa": 100.0,
            "target_roas": 2.0,
            "bid_cap_ratio": 5.0,  # cap far above any reachable bid
        }
        strong = recommend_bid(observed_roas=4.0, **kwargs)
        on_target = recommend_bid(observed_roas=2.0, **kwargs)

        assert strong.base_multiplier == pytest.approx(1.3)
        assert on_target.base_multiplier == pytest.approx(1.0)
        assert strong.bid_cpm == pytest.approx(on_target.bid_cpm * 1.3, rel=1e-3)

    def test_bid_is_capped_at_a_fraction_of_target_cpa_value(self) -> None:
        result = recommend_bid(
            campaign_id="c1",
            predicted_ctr=0.03,
            predicted_cvr=0.05,
            observed_roas=4.0,
            impressions=50_000,
            target_cpa=100.0,
            target_roas=2.0,
            bid_cap_ratio=0.8,
        )
        expected_cap = 0.03 * 0.05 * 100.0 * 1000.0 * 0.8  # eCPM * cap ratio
        assert result.bid_cpm == pytest.approx(expected_cap, rel=1e-3)
        assert "capped" in result.reasoning

    def test_bid_never_falls_below_the_minimum(self) -> None:
        result = recommend_bid(
            campaign_id="c1",
            predicted_ctr=0.0,
            predicted_cvr=0.0,
            observed_roas=0.0,
            impressions=0,
        )
        assert result.bid_cpm >= MIN_BID

    def test_learning_phase_eases_the_bid(self) -> None:
        common = {
            "campaign_id": "c1",
            "predicted_ctr": 0.03,
            "predicted_cvr": 0.05,
            "observed_roas": 2.0,
            "target_cpa": 100.0,
            "target_roas": 2.0,
            "bid_cap_ratio": 5.0,
        }
        mature = recommend_bid(impressions=50_000, **common)
        learning = recommend_bid(impressions=0, **common)
        assert mature.multiplier == pytest.approx(1.0)
        assert learning.multiplier == pytest.approx(0.85)

    def test_confidence_grows_with_evidence(self) -> None:
        common = {
            "campaign_id": "c1",
            "predicted_ctr": 0.03,
            "predicted_cvr": 0.05,
            "observed_roas": 2.0,
        }
        thin = recommend_bid(impressions=100, **common)
        rich = recommend_bid(impressions=50_000, **common)
        assert rich.confidence > thin.confidence
        assert 0.0 <= thin.confidence <= 0.98

    def test_zero_target_cpa_falls_back_to_the_default(self) -> None:
        result = recommend_bid(
            campaign_id="c1",
            predicted_ctr=0.03,
            predicted_cvr=0.05,
            observed_roas=2.0,
            impressions=50_000,
            target_cpa=0.0,
        )
        assert result.ecpm > 0.0

    def test_max_cpc_is_bounded_by_the_cap_ratio(self) -> None:
        result = recommend_bid(
            campaign_id="c1",
            predicted_ctr=0.03,
            predicted_cvr=0.90,
            observed_roas=4.0,
            impressions=50_000,
            target_cpa=100.0,
            bid_cap_ratio=0.8,
        )
        assert result.max_cpc <= 100.0 * 0.8 + 1e-9

    def test_reasoning_mentions_learning_and_roas(self) -> None:
        result = recommend_bid(
            campaign_id="c1",
            predicted_ctr=0.03,
            predicted_cvr=0.05,
            observed_roas=0.4,
            impressions=10,
            bid_cap_ratio=5.0,
        )
        assert "learning phase" in result.reasoning
        assert result.base_multiplier == pytest.approx(0.6)
