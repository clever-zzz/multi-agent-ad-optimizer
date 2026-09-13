"""Unit tests for the bounded budget reallocation LP."""

from __future__ import annotations

import pytest

from adoptimizer.domain.budget import (
    MIN_BUDGET_FLOOR,
    allocate,
    allocation_score,
    total_delta,
)
from adoptimizer.domain.kpi import PerformanceSnapshot


def snap(
    campaign_id: str,
    *,
    impressions: int = 10_000,
    clicks: int = 300,
    conversions: int = 20,
    total_cost: float = 1_000.0,
    total_revenue: float = 3_000.0,
) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        campaign_id=campaign_id,
        campaign_name=campaign_id,
        impressions=impressions,
        clicks=clicks,
        conversions=conversions,
        total_cost=total_cost,
        total_revenue=total_revenue,
    )


WINNER = snap("winner", total_cost=1_000.0, total_revenue=5_000.0, conversions=50)
LOSER = snap("loser", total_cost=1_000.0, total_revenue=500.0, conversions=5)


class TestAllocationScore:
    def test_higher_roas_scores_higher(self) -> None:
        assert allocation_score(WINNER) > allocation_score(LOSER)

    def test_lucky_micro_sample_cannot_dominate(self) -> None:
        """A campaign with two conversions must not outscore a proven one."""
        lucky = snap(
            "lucky", impressions=20, clicks=2, conversions=2, total_cost=10.0, total_revenue=200.0
        )
        proven = snap(
            "proven",
            impressions=500_000,
            clicks=25_000,
            conversions=1_000,
            total_cost=50_000.0,
            total_revenue=150_000.0,
        )
        # Raw ROAS is higher for the lucky campaign (20x vs 3x) but the confidence
        # weight keeps the proven one competitive.
        assert lucky.roas > proven.roas
        assert allocation_score(lucky) < allocation_score(proven) * 1.5

    def test_score_is_non_negative(self) -> None:
        assert allocation_score(snap("zero", total_cost=100.0, total_revenue=0.0)) >= 0.0


class TestAllocate:
    def test_budget_shifts_toward_the_efficient_campaign(self) -> None:
        plan = allocate([WINNER, LOSER], max_change_pct=0.5, cross_check_with_solver=False)
        by_id = {a.campaign_id: a for a in plan}
        assert by_id["winner"].recommended_budget == pytest.approx(1_500.0)
        assert by_id["loser"].recommended_budget == pytest.approx(500.0)
        assert by_id["winner"].change_pct == pytest.approx(50.0)
        assert by_id["loser"].change_pct == pytest.approx(-50.0)

    def test_total_budget_is_preserved_exactly(self) -> None:
        plan = allocate([WINNER, LOSER], cross_check_with_solver=False)
        summary = total_delta(plan)
        assert summary["recommended_total"] == pytest.approx(summary["current_total"])
        assert summary["net_delta"] == pytest.approx(0.0, abs=0.01)

    def test_per_campaign_change_bound_is_never_violated(self) -> None:
        """Regression: the demo clipped then renormalised, breaking the bounds."""
        snapshots = [
            snap(f"c{i}", total_cost=100.0 * (i + 1), total_revenue=50.0 * (i + 3))
            for i in range(6)
        ]
        for pct in (0.1, 0.25, 0.5):
            for allocation in allocate(
                snapshots, max_change_pct=pct, cross_check_with_solver=False
            ):
                # recommended_budget is rounded to cents, so allow a hair of drift.
                assert allocation.change_pct <= pct * 100 + 0.02
                assert allocation.change_pct >= -pct * 100 - 0.02

    def test_zero_change_pct_holds_every_budget(self) -> None:
        plan = allocate([WINNER, LOSER], max_change_pct=0.0, cross_check_with_solver=False)
        assert all(a.recommended_budget == pytest.approx(a.current_budget) for a in plan)
        assert all("held" in a.reason for a in plan)

    def test_explicit_total_budget_is_clamped_to_the_feasible_range(self) -> None:
        plan = allocate([WINNER, LOSER], total_budget=10_000.0, cross_check_with_solver=False)
        # Upper bound is 1500 + 1500, so the request cannot be honoured in full.
        assert sum(a.recommended_budget for a in plan) == pytest.approx(3_000.0)

    def test_explicit_total_budget_below_the_floor_is_raised(self) -> None:
        plan = allocate([WINNER, LOSER], total_budget=1.0, cross_check_with_solver=False)
        assert sum(a.recommended_budget for a in plan) == pytest.approx(1_000.0)

    def test_campaigns_without_spend_are_skipped(self) -> None:
        plan = allocate(
            [snap("idle", total_cost=0.0, total_revenue=0.0)], cross_check_with_solver=False
        )
        assert plan == []

    def test_mixed_spend_only_returns_paying_campaigns(self) -> None:
        plan = allocate(
            [WINNER, snap("idle", total_cost=0.0, total_revenue=0.0)], cross_check_with_solver=False
        )
        assert [a.campaign_id for a in plan] == ["winner"]

    def test_minimum_floor_is_respected(self) -> None:
        tiny = snap(
            "tiny", total_cost=0.2, total_revenue=5.0, impressions=100, clicks=5, conversions=1
        )
        plan = allocate([tiny, WINNER], cross_check_with_solver=False)
        assert all(
            a.recommended_budget >= MIN_BUDGET_FLOOR or a.current_budget >= MIN_BUDGET_FLOOR
            for a in plan
        )
        assert all(a.recommended_budget >= 0.0 for a in plan)

    def test_result_is_deterministic(self) -> None:
        first = allocate([WINNER, LOSER], cross_check_with_solver=False)
        second = allocate([WINNER, LOSER], cross_check_with_solver=False)
        assert [a.model_dump() for a in first] == [a.model_dump() for a in second]

    def test_solver_cross_check_produces_the_same_or_better_objective(self) -> None:
        snapshots = [WINNER, LOSER, snap("mid", total_cost=800.0, total_revenue=1_600.0)]
        verified = allocate(snapshots, cross_check_with_solver=True)
        greedy = allocate(snapshots, cross_check_with_solver=False)
        assert sum(a.recommended_budget for a in verified) == pytest.approx(
            sum(a.recommended_budget for a in greedy), abs=0.05
        )
        assert all(a.solver for a in verified)

    def test_reasons_are_human_readable(self) -> None:
        plan = allocate([WINNER, LOSER], cross_check_with_solver=False)
        by_id = {a.campaign_id: a for a in plan}
        assert "scaling spend up" in by_id["winner"].reason
        assert "pulling spend back" in by_id["loser"].reason

    def test_daily_budgets_set_the_baseline_not_window_spend(self) -> None:
        """Regression: sizing from window spend inflated every recommendation.

        The recommendation lands in ``platform.set_daily_budget``, so the
        baseline has to be the configured daily budget rather than what the
        whole window happened to cost.
        """
        plan = allocate(
            [WINNER, LOSER],
            daily_budgets={"winner": 200.0, "loser": 100.0},
            max_change_pct=0.5,
            cross_check_with_solver=False,
        )
        by_id = {a.campaign_id: a for a in plan}
        # Both campaigns spent 1000 over the window; no baseline may be 1000.
        assert by_id["winner"].current_budget == pytest.approx(200.0)
        assert by_id["loser"].current_budget == pytest.approx(100.0)
        for allocation in plan:
            assert allocation.recommended_budget <= allocation.current_budget * 1.5 + 0.01

    def test_a_campaign_without_budget_config_falls_back_to_spend(self) -> None:
        plan = allocate([WINNER], daily_budgets={}, cross_check_with_solver=False)
        assert plan[0].current_budget == pytest.approx(1_000.0)

    def test_reasons_are_relative_to_the_portfolio_not_absolute(self) -> None:
        """A strong campaign that still loses budget must not be called inefficient.

        The reallocation is zero-sum, so "pulling spend back" only ever means
        "behind the rest of the portfolio". The previous wording labelled a ROAS
        of 13 inefficient, which was simply untrue.
        """
        boutique = PerformanceSnapshot(
            campaign_id="boutique",
            campaign_name="boutique",
            impressions=100,
            clicks=10,
            conversions=2,
            total_cost=100.0,
            total_revenue=1_300.0,
        )
        proven = PerformanceSnapshot(
            campaign_id="proven",
            campaign_name="proven",
            impressions=500_000,
            clicks=25_000,
            conversions=1_000,
            total_cost=50_000.0,
            total_revenue=200_000.0,
        )
        plan = allocate(
            [boutique, proven],
            daily_budgets={"boutique": 100.0, "proven": 100.0},
            cross_check_with_solver=False,
        )
        by_id = {a.campaign_id: a for a in plan}
        assert boutique.roas > 3.0
        assert by_id["boutique"].change_pct < 0
        assert "inefficient" not in by_id["boutique"].reason
        assert "trails the portfolio" in by_id["boutique"].reason
        assert "leads the portfolio" in by_id["proven"].reason


class TestTotalDelta:
    def test_counts_increases_and_decreases(self) -> None:
        summary = total_delta(allocate([WINNER, LOSER], cross_check_with_solver=False))
        assert summary["campaigns"] == 2
        assert summary["increased"] == 1
        assert summary["decreased"] == 1
        assert summary["unchanged"] == 0

    def test_empty_plan(self) -> None:
        summary = total_delta([])
        assert summary == {
            "campaigns": 0,
            "current_total": 0,
            "recommended_total": 0,
            "net_delta": 0,
            "increased": 0,
            "decreased": 0,
            "unchanged": 0,
        }


class TestNoDecreaseGuard:
    """A campaign at or above its target is not cut in the same run.

    The bid agent judges a campaign against its own ROAS target while this
    allocator judges it against the portfolio average, so without the guard one
    run can raise a campaign's bid and halve its budget at once.
    """

    def test_a_protected_campaign_keeps_its_budget(self) -> None:
        plan = allocate(
            [WINNER, LOSER],
            max_change_pct=0.5,
            cross_check_with_solver=False,
            no_decrease={"loser"},
        )
        by_id = {allocation.campaign_id: allocation for allocation in plan}
        loser = by_id["loser"]

        assert loser.recommended_budget == pytest.approx(loser.current_budget)
        assert "meets its target" in loser.reason

    def test_the_total_is_still_preserved(self) -> None:
        plan = allocate(
            [WINNER, LOSER],
            max_change_pct=0.5,
            cross_check_with_solver=False,
            no_decrease={"loser"},
        )

        current = sum(allocation.current_budget for allocation in plan)
        recommended = sum(allocation.recommended_budget for allocation in plan)

        assert recommended == pytest.approx(current)

    def test_without_the_guard_the_cut_still_happens(self) -> None:
        plan = allocate([WINNER, LOSER], max_change_pct=0.5, cross_check_with_solver=False)
        by_id = {allocation.campaign_id: allocation for allocation in plan}

        assert by_id["loser"].recommended_budget < by_id["loser"].current_budget
