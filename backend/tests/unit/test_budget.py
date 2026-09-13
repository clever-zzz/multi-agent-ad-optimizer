"""Unit tests for the bounded budget reallocation LP."""

from __future__ import annotations

import numpy as np
import pytest

from adoptimizer.domain.budget import (
    EFFICIENCY_REFERENCE_ROAS,
    MIN_BUDGET_FLOOR,
    _fill_order,
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

# A portfolio where every campaign is protected and every budget is the same
# size, so the holds alone leave the allocator nothing to move.
HEALTHY = snap("healthy", total_cost=1_000.0, total_revenue=9_000.0, conversions=100)
MARGINAL = snap("marginal", total_cost=1_000.0, total_revenue=1_500.0, conversions=100)
EVEN_BUDGETS = {"healthy": 1_000.0, "marginal": 1_000.0}


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


class TestEfficiencyCompression:
    """Scores must keep separating campaigns well above the reference ROAS.

    The efficiency term used to be a linear ratio clipped at 2x, so every
    campaign above ROAS 6 scored identically. Six of the eight seeded campaigns
    landed on exactly 2.0, which left the allocator nothing to rank on.
    """

    def test_the_reference_roas_still_scores_one(self) -> None:
        """Calibration is unchanged, so scores stay comparable across the fix."""
        mature = snap("mature", total_cost=1_000.0, total_revenue=3_000.0)

        assert mature.roas == pytest.approx(EFFICIENCY_REFERENCE_ROAS)
        assert allocation_score(mature) == pytest.approx(1.0)

    def test_campaigns_above_the_old_cap_stay_distinguishable(self) -> None:
        """Regression: ROAS 6 and ROAS 30 both scored 2.0 and could not be ranked."""
        roas_values = (6.0, 8.0, 12.0, 16.0, 20.0, 30.0)
        snapshots = [
            snap("c" + str(i), total_cost=1_000.0, total_revenue=1_000.0 * roas)
            for i, roas in enumerate(roas_values)
        ]
        scores = [allocation_score(snapshot) for snapshot in snapshots]

        assert len(set(scores)) == len(scores)
        assert scores == sorted(scores)

    def test_each_extra_unit_of_roas_is_worth_less_than_the_last(self) -> None:
        """Diminishing returns, which is what justifies compression over a cap."""

        def score_at(roas: float) -> float:
            return allocation_score(snap("c", total_cost=1_000.0, total_revenue=1_000.0 * roas))

        assert score_at(4.0) - score_at(3.0) > score_at(31.0) - score_at(30.0)


class TestTieBreak:
    """Equal scores must not make the plan depend on the order rows arrived in."""

    def test_the_fill_order_ranks_on_score_then_roas_then_id(self) -> None:
        ids = ["bbb", "aaa", "ccc"]
        scores = np.array([1.0, 2.0, 2.0])
        roas = np.array([9.0, 4.0, 7.0])

        # aaa and ccc tie on score, so ccc's higher ROAS goes first, then aaa,
        # and bbb's lower score puts it last despite the best ROAS.
        assert _fill_order(ids, scores, roas) == [2, 1, 0]

    def test_a_full_tie_falls_back_to_the_campaign_id(self) -> None:
        ids = ["zzz", "aaa"]
        scores = np.array([1.5, 1.5])
        roas = np.array([4.0, 4.0])

        assert _fill_order(ids, scores, roas) == [1, 0]

    def test_the_plan_is_the_same_whichever_order_the_campaigns_come_in(self) -> None:
        """Regression: an exact score tie fell through to array position.

        Two campaigns with identical performance differ only in budget, so the
        greedy fill hands the spare headroom to whichever one it visits first.
        Visiting them in caller order meant the cut landed on whichever campaign
        the database happened to return first.
        """
        twin_a = snap("aaa", total_cost=1_000.0, total_revenue=9_000.0, conversions=100)
        twin_b = snap("zzz", total_cost=1_000.0, total_revenue=9_000.0, conversions=100)
        assert allocation_score(twin_a) == allocation_score(twin_b)

        budgets = {"aaa": 1_000.0, "zzz": 100.0}
        forward = allocate([twin_a, twin_b], daily_budgets=budgets, cross_check_with_solver=False)
        backward = allocate([twin_b, twin_a], daily_budgets=budgets, cross_check_with_solver=False)

        plan = {item.campaign_id: item.recommended_budget for item in forward}
        assert plan == {item.campaign_id: item.recommended_budget for item in backward}
        assert plan["aaa"] == pytest.approx(1_050.0)
        assert plan["zzz"] == pytest.approx(50.0)


class TestHoldDeadlock:
    """Holding every campaign must not silence the allocator.

    A hold pins a campaign's floor to its current budget, so holding all of them
    leaves the greedy fill no headroom and the plan degenerates into "change
    nothing". That is what the ROAS-target guard did to every seeded run: the
    whole portfolio sat far above its 2.0 target, all eight campaigns were
    protected, and the budget agent proposed nothing while the run still
    reported success.
    """

    def test_holding_every_campaign_still_produces_a_plan(self) -> None:
        plan = allocate(
            [HEALTHY, MARGINAL],
            daily_budgets=EVEN_BUDGETS,
            cross_check_with_solver=False,
            no_decrease={"healthy", "marginal"},
        )
        by_id = {item.campaign_id: item for item in plan}

        assert by_id["marginal"].change_pct < 0, "the deadlock was never released"
        assert by_id["healthy"].change_pct > 0
        assert sum(item.delta for item in plan) == pytest.approx(0.0, abs=0.01)

    def test_only_the_weakest_hold_is_released(self) -> None:
        """The guard keeps protecting everything it possibly can."""
        snapshots = [
            snap("c" + str(i), total_cost=1_000.0, total_revenue=1_000.0 * (i + 2), conversions=100)
            for i in range(4)
        ]
        budgets = {item.campaign_id: 1_000.0 for item in snapshots}
        plan = allocate(
            snapshots,
            daily_budgets=budgets,
            cross_check_with_solver=False,
            no_decrease={item.campaign_id for item in snapshots},
        )
        moved = {item.campaign_id for item in plan if item.delta != 0}

        # Freeing the single weakest campaign is all the headroom the fill
        # needs, so it funds the strongest and leaves the middle two protected.
        assert moved == {"c0", "c3"}

    def test_a_total_below_current_spend_releases_every_hold(self) -> None:
        """Freeing one campaign is not always enough, so the release keeps going.

        Cutting the portfolio total below what it spends today needs more than
        one protected budget to give way. Stopping after the first release would
        leave the allocator deadlocked and the plan empty.
        """
        snapshots = [
            snap(
                "c" + str(i),
                total_cost=1_000.0,
                total_revenue=1_000.0 * (i + 2),
                conversions=100,
            )
            for i in range(3)
        ]
        plan = allocate(
            snapshots,
            total_budget=1_600.0,
            cross_check_with_solver=False,
            no_decrease={item.campaign_id for item in snapshots},
        )
        by_id = {item.campaign_id: item for item in plan}

        assert sum(item.recommended_budget for item in plan) == pytest.approx(1_600.0)
        # Nothing survived as protected, so nothing may claim the hold wording.
        assert all("meets its target" not in item.reason for item in plan)
        assert by_id["c2"].recommended_budget > by_id["c0"].recommended_budget

    def test_a_zero_change_budget_releases_every_hold_and_still_moves_nothing(self) -> None:
        """With no room to move there is no deadlock to break, only nothing to do."""
        plan = allocate(
            [HEALTHY, MARGINAL],
            daily_budgets=EVEN_BUDGETS,
            max_change_pct=0.0,
            cross_check_with_solver=False,
            no_decrease={"healthy", "marginal"},
        )

        assert all(item.delta == 0 for item in plan)
        assert all("meets its target" not in item.reason for item in plan)

    def test_a_released_hold_does_not_claim_its_budget_was_held(self) -> None:
        plan = allocate(
            [HEALTHY, MARGINAL],
            daily_budgets=EVEN_BUDGETS,
            cross_check_with_solver=False,
            no_decrease={"healthy", "marginal"},
        )
        by_id = {item.campaign_id: item for item in plan}

        assert "meets its target" not in by_id["marginal"].reason
        assert "trails the portfolio" in by_id["marginal"].reason

    def test_a_held_campaign_that_gains_budget_says_it_was_funded(self) -> None:
        """Saying "held rather than cut" about a budget the plan just raised is untrue."""
        plan = allocate(
            [HEALTHY, MARGINAL],
            daily_budgets=EVEN_BUDGETS,
            cross_check_with_solver=False,
            no_decrease={"healthy", "marginal"},
        )
        by_id = {item.campaign_id: item for item in plan}

        assert by_id["healthy"].change_pct > 5
        assert "held rather than cut" not in by_id["healthy"].reason
        assert "leads the portfolio" in by_id["healthy"].reason
