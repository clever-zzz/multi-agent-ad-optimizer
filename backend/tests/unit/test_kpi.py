"""Unit tests for KPI snapshots and portfolio summaries."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from adoptimizer.domain.kpi import (
    PerformanceSnapshot,
    health_score,
    health_status,
    snapshot_from_row,
    summarize,
)


def snap(**kwargs: object) -> PerformanceSnapshot:
    base: dict[str, object] = {
        "campaign_id": "c1",
        "impressions": 10_000,
        "clicks": 300,
        "conversions": 20,
        "total_cost": 500.0,
        "total_revenue": 1_500.0,
    }
    base.update(kwargs)
    return PerformanceSnapshot(**base)  # type: ignore[arg-type]


class TestFunnelValidation:
    def test_clicks_above_impressions_rejected(self) -> None:
        with pytest.raises(ValidationError, match="clicks cannot exceed impressions"):
            snap(impressions=100, clicks=200)

    def test_conversions_above_clicks_rejected(self) -> None:
        with pytest.raises(ValidationError, match="conversions cannot exceed clicks"):
            snap(clicks=10, conversions=20)

    def test_negative_counters_rejected(self) -> None:
        with pytest.raises(ValidationError):
            snap(impressions=-1)

    def test_model_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            snap().impressions = 5  # type: ignore[misc]


class TestDerivedMetrics:
    def test_rates(self) -> None:
        s = snap()
        assert s.ctr == pytest.approx(0.03)
        assert s.cvr == pytest.approx(20 / 300)
        assert s.cpa == pytest.approx(25.0)
        assert s.roas == pytest.approx(3.0)
        assert s.cpc == pytest.approx(500 / 300, abs=1e-4)
        assert s.cpm == pytest.approx(50.0)

    def test_cpa_is_none_not_infinity_without_conversions(self) -> None:
        """Regression: the demo returned inf, which is not valid strict JSON."""
        s = snap(conversions=0, clicks=10)
        assert s.cpa is None
        assert json.loads(s.model_dump_json())["cpa"] is None

    def test_zero_spend_roas_is_zero_not_nan(self) -> None:
        s = snap(total_cost=0.0, total_revenue=0.0)
        assert s.roas == 0.0
        assert s.cpa is None or s.cpa == 0.0

    def test_predicted_rates_are_shrunk_toward_the_prior(self) -> None:
        s = snap(impressions=100, clicks=10, conversions=1)
        # Raw CTR is 10%; the shrunk estimate must be far closer to the 2.5% prior.
        assert s.ctr == pytest.approx(0.10)
        assert s.predicted_ctr < s.ctr
        assert s.predicted_ctr > 0.025

    def test_predicted_rate_converges_with_volume(self) -> None:
        large = snap(impressions=1_000_000, clicks=30_000, conversions=1_000)
        assert large.predicted_ctr == pytest.approx(0.03, abs=1e-3)

    def test_ctr_lower_bound_is_conservative(self) -> None:
        s = snap()
        assert s.ctr_lower_bound < s.ctr

    def test_has_volume(self) -> None:
        assert snap(impressions=1_000).has_volume(500) is True
        assert snap(impressions=100, clicks=30, conversions=5).has_volume(500) is False

    def test_computed_fields_survive_serialization(self) -> None:
        """Regression: plain properties were dropped by model_dump in the demo."""
        payload = snap().model_dump()
        for key in ("ctr", "cvr", "cpa", "roas", "cpc", "cpm", "predicted_ctr"):
            assert key in payload

    def test_dump_then_validate_round_trip_is_accepted(self) -> None:
        """Regression: extra="forbid" rejected the computed fields model_dump emits.

        Agents shuttle snapshots through the graph state and the LangGraph
        checkpointer as plain dicts, so this single hop used to take every agent
        node down at once.
        """
        original = snap()
        assert PerformanceSnapshot.model_validate(original.model_dump()) == original
        assert (
            PerformanceSnapshot.model_validate(json.loads(original.model_dump_json())) == original
        )

    def test_round_trip_tolerance_does_not_swallow_typos(self) -> None:
        """Only derived names are dropped; genuinely unknown input stays refused."""
        with pytest.raises(ValidationError):
            PerformanceSnapshot.model_validate({"campaign_id": "c1", "impressons": 5})


class TestPortfolioSummary:
    def test_aggregation(self) -> None:
        summary = summarize([snap(campaign_id="a"), snap(campaign_id="b", total_cost=250.0)])
        assert summary.campaigns == 2
        assert summary.impressions == 20_000
        assert summary.total_cost == pytest.approx(750.0)
        assert summary.ctr == pytest.approx(0.03)

    def test_empty_portfolio(self) -> None:
        summary = summarize([])
        assert summary.campaigns == 0
        assert summary.cpa is None
        assert summary.roas == 0.0

    def test_cpa_none_when_no_conversions(self) -> None:
        summary = summarize([snap(conversions=0, clicks=10)])
        assert summary.cpa is None


class TestHealthScore:
    def test_strong_portfolio_is_healthy(self) -> None:
        summary = summarize(
            [
                snap(
                    impressions=20_000,
                    clicks=1_000,
                    conversions=100,
                    total_cost=5_000.0,
                    total_revenue=20_000.0,
                )
            ]
        )
        score = health_score(summary)
        assert score >= 70
        assert health_status(score) == "healthy"

    def test_weak_portfolio_is_critical(self) -> None:
        summary = summarize(
            [
                snap(
                    impressions=20_000,
                    clicks=50,
                    conversions=1,
                    total_cost=5_000.0,
                    total_revenue=100.0,
                )
            ]
        )
        score = health_score(summary)
        assert health_status(score) == "critical"

    def test_score_is_bounded(self) -> None:
        assert 0.0 <= health_score(summarize([snap()])) <= 100.0

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(95, "healthy"), (70, "healthy"), (50, "warning"), (45, "warning"), (10, "critical")],
    )
    def test_status_bands(self, score: float, expected: str) -> None:
        assert health_status(score) == expected


class TestSnapshotFromRow:
    def test_builds_from_a_warehouse_row(self) -> None:
        row = {
            "campaign_id": 42,
            "campaign_name": "Spring",
            "impressions": "1000",
            "clicks": None,
            "conversions": 0,
            "total_cost": 100.0,
            "total_revenue": 400.0,
        }
        s = snapshot_from_row(row)
        assert s.campaign_id == "42"
        assert s.campaign_name == "Spring"
        assert s.impressions == 1000
        assert s.clicks == 0  # None coerced to a usable zero
        assert s.roas == pytest.approx(4.0)

    def test_missing_optional_columns_default_to_zero(self) -> None:
        s = snapshot_from_row({"campaign_id": "x"})
        assert (s.impressions, s.clicks, s.conversions) == (0, 0, 0)
        assert s.window_start is None

    def test_missing_campaign_id_raises(self) -> None:
        with pytest.raises(KeyError):
            snapshot_from_row({"impressions": 10})
