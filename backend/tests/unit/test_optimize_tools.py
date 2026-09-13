"""Unit tests for the optimizer rehearsing its own proposals.

The optimizer is the only agent that proposes writes, so the dry run it performs
before handing a proposal over is the difference between "the network would take
this" and "a human was asked to approve something the network would reject".
These tests pin the annotation shape the critic and the approval screen both
read, and the cases where rehearsing is deliberately not attempted at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from adoptimizer.agents import optimize as optimize_module
from adoptimizer.agents.base import AgentContext
from adoptimizer.agents.optimize import (
    BID_MOVE_THRESHOLD,
    MAX_PREFLIGHTS_PER_ITERATION,
    OptimizeAgent,
)
from adoptimizer.core.config import (
    DataMode,
    LLMProvider,
    LLMSettings,
    OptimizationSettings,
    ToolSettings,
)
from adoptimizer.domain.enums import ActionType, AlertRule, Platform
from adoptimizer.domain.kpi import PerformanceSnapshot
from adoptimizer.infra.ads.mock import MockAdsClient
from adoptimizer.infra.ads.registry import PlatformRegistry
from adoptimizer.llm.gateway import build_gateway
from adoptimizer.orchestrator.events import EventBus
from adoptimizer.orchestrator.state import AgentState, initial_state
from adoptimizer.tools import ToolExecutor, build_tool_executor

RUN_ID = "run_opt_tools"
CAMP_A = "camp_a"
NEVER_SYNCED = "camp_local"
BAD_CREATIVE = "cre_bad"
UNKNOWN_NETWORK = "network_we_do_not_support_yet"

# The annotation the critic reads. Exactly these keys: the critic treats a
# missing preflight as "not rehearsed" and a present one as a verdict, so an
# extra or absent field silently changes what survives to the approval screen.
ANNOTATION_KEYS = {"tool", "outcome", "refused", "blocking", "error"}


def build_tools(**overrides: Any) -> tuple[ToolExecutor, MockAdsClient]:
    """A real executor over the recording mock adapter."""
    adapter = MockAdsClient(Platform.MOCK)
    registry = PlatformRegistry({Platform.MOCK: adapter}, data_mode=DataMode.MOCK)
    return build_tool_executor(registry, ToolSettings(**overrides)), adapter


def make_context(executor: ToolExecutor | None, **overrides: Any) -> AgentContext:
    """A run context that knows about one synced and one never-synced campaign."""
    return AgentContext(
        run_id=RUN_ID,
        optimization=OptimizationSettings(use_convex_solver=False),
        gateway=build_gateway(LLMSettings(provider=LLMProvider.MOCK, cache_enabled=False)),
        bus=EventBus(),
        tools=executor,
        campaign_refs={
            CAMP_A: {
                "platform": Platform.MOCK.value,
                "external_id": "ext_camp_a",
                "name": "Campaign A",
                "status": "active",
            },
            NEVER_SYNCED: {
                "platform": Platform.MOCK.value,
                "external_id": "",
                "name": "Never synced to a network",
                "status": "active",
            },
        },
        **overrides,
    )


def alert(
    rule: AlertRule,
    campaign_id: str = CAMP_A,
    *,
    severity: str = "critical",
    message: str = "CPA 141 against a target of 80",
) -> dict[str, Any]:
    """One anomaly alert in the shape the monitor emits."""
    return {
        "rule": rule.value,
        "campaign_id": campaign_id,
        "severity": severity,
        "message": message,
    }


def alert_state(*alerts: dict[str, Any], campaign_ids: list[str] | None = None) -> AgentState:
    state = initial_state(run_id=RUN_ID, max_iterations=2, campaign_ids=campaign_ids or [CAMP_A])
    state["alerts"] = list(alerts)
    return state


def proposal(
    action_type: ActionType, campaign_id: str = CAMP_A, **overrides: Any
) -> dict[str, Any]:
    """A proposal built by hand, for the cases run() cannot easily produce."""
    action: dict[str, Any] = {
        "id": "act_" + action_type.value,
        "campaign_id": campaign_id,
        "creative_id": None,
        "action_type": action_type.value,
        "status": "proposed",
        "after_value": "",
        "reason": "proposed by a test",
        "confidence": 0.8,
        "iteration": 1,
    }
    action.update(overrides)
    return action


def only(update: dict[str, Any], action_type: ActionType) -> dict[str, Any]:
    matches = [
        action
        for action in update["optimization_actions"]
        if action["action_type"] == action_type.value
    ]
    assert len(matches) == 1, update["optimization_actions"]
    return matches[0]


@pytest.fixture
def agent() -> OptimizeAgent:
    return OptimizeAgent()


class TestWriteProposalsAreRehearsed:
    """A proposal that moves money is dry-run before anyone is asked to approve it."""

    async def test_a_pause_proposal_comes_back_annotated(self, agent: OptimizeAgent) -> None:
        executor, adapter = build_tools()
        context = make_context(executor)

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        action = only(update, ActionType.PAUSE_CAMPAIGN)
        assert set(action["preflight"]) == ANNOTATION_KEYS
        assert action["preflight"]["tool"] == "platform.pause_campaign"
        assert action["preflight"]["outcome"] == "dry_run"
        assert action["preflight"]["refused"] is False
        assert action["preflight"]["blocking"] is False
        assert action["preflight"]["error"] is None
        assert adapter.calls == []

    async def test_the_record_carries_the_network_coordinates(self, agent: OptimizeAgent) -> None:
        executor, _ = build_tools()
        context = make_context(executor)

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        records = update["tool_preflights"]
        assert len(records) == 1
        record = records[0]
        assert record["tool"] == "platform.pause_campaign"
        assert record["campaign_id"] == CAMP_A
        assert record["target_id"] == "ext_camp_a"
        assert record["action_id"] == only(update, ActionType.PAUSE_CAMPAIGN)["id"]
        assert record["iteration"] == 1
        assert record["dry_run"] is True
        assert record["blocking"] is False
        assert record["duration_ms"] >= 0.0

    async def test_a_budget_proposal_rehearses_the_number_it_will_send(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(executor)
        action = proposal(ActionType.ADJUST_BUDGET, after_value="1500.00")

        records = await agent._preflight([action], context, 1)

        assert records[0]["tool"] == "platform.set_daily_budget"
        assert action["preflight"]["outcome"] == "dry_run"
        # The dry-run payload is the contract: it is what an operator reads to
        # check the agent meant the number it wrote down.
        arguments = executor.usage(RUN_ID)["by_tool"]
        assert arguments == {"platform.set_daily_budget": 1}

    async def test_the_rehearsal_is_counted_as_a_write_that_touched_nothing(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(executor)

        await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        usage = executor.usage(RUN_ID)
        assert usage["invocations"] == 1
        assert usage["writes"] == 1
        assert usage["dry_runs"] == 1
        assert usage["refusals"] == 0
        assert usage["by_outcome"] == {"dry_run": 1}
        # Agent traffic is what the per-run budget exists to bound.
        assert usage["calls"] == 1

    async def test_a_creative_proposal_is_rehearsed_against_its_own_tool(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(
            executor,
            existing_creatives={
                CAMP_A: [
                    {
                        "creative_id": BAD_CREATIVE,
                        "status": "active",
                        "ab_group": "variant",
                        "impressions": 20000,
                        "clicks": 20,
                        "conversions": 0,
                        "cost": 800.0,
                        "revenue": 0.0,
                    }
                ]
            },
        )

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        action = only(update, ActionType.PAUSE_CREATIVE)
        assert action["creative_id"] == BAD_CREATIVE
        assert action["preflight"]["tool"] == "platform.pause_creative"
        record = next(
            item for item in update["tool_preflights"] if item["tool"] == "platform.pause_creative"
        )
        assert record["target_id"] == BAD_CREATIVE


class TestProposalsThatAreNotRehearsed:
    """No verdict is different from a good verdict, and must not look like one."""

    async def test_advisory_remedies_have_nothing_to_rehearse(self, agent: OptimizeAgent) -> None:
        executor, adapter = build_tools()
        context = make_context(executor)

        update = await agent.run(
            alert_state(alert(AlertRule.LOW_CTR), alert(AlertRule.LOW_ROAS)), context
        )

        types = sorted(action["action_type"] for action in update["optimization_actions"])
        assert types == sorted([ActionType.ADJUST_BID.value, ActionType.REFRESH_CREATIVE.value])
        assert all("preflight" not in action for action in update["optimization_actions"])
        assert update["tool_preflights"] == []
        assert executor.usage(RUN_ID)["invocations"] == 0
        assert adapter.calls == []

    async def test_a_campaign_that_was_never_synced_is_skipped_not_guessed(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(executor)

        update = await agent.run(alert_state(alert(AlertRule.BURN_RATE, NEVER_SYNCED)), context)

        action = only(update, ActionType.PAUSE_CAMPAIGN)
        assert action["campaign_id"] == NEVER_SYNCED
        assert "preflight" not in action
        assert update["tool_preflights"] == []

    async def test_a_campaign_level_creative_proxy_names_no_asset(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(executor)
        action = proposal(ActionType.PAUSE_CREATIVE, creative_id=None)

        assert agent._preflight_call(action, context) is None
        assert await agent._preflight([action], context, 1) == []
        assert "preflight" not in action

    async def test_an_unknown_action_type_is_ignored_rather_than_sent(
        self, agent: OptimizeAgent
    ) -> None:
        executor, _ = build_tools()
        context = make_context(executor)
        action = proposal(ActionType.ADJUST_BUDGET)
        action["action_type"] = "launch_the_campaign"

        assert agent._preflight_call(action, context) is None
        assert await agent._preflight([action], context, 1) == []

    async def test_rehearsal_is_skipped_when_the_tool_layer_is_off(
        self, agent: OptimizeAgent
    ) -> None:
        executor, adapter = build_tools(enabled=False)
        context = make_context(executor)

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        assert update["tool_preflights"] == []
        assert all("preflight" not in action for action in update["optimization_actions"])
        # The proposal still exists: a guardrail being off is a deployment state,
        # not a reason to stop optimising.
        assert len(update["optimization_actions"]) == 1
        assert adapter.calls == []

    async def test_proposals_survive_a_deployment_with_no_tool_layer(
        self, agent: OptimizeAgent
    ) -> None:
        context = make_context(None)

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        assert len(update["optimization_actions"]) == 1
        assert update["tool_preflights"] == []


class TestARefusedRehearsal:
    """A proposal the platform would reject must say so on its own record."""

    async def test_a_proposal_the_schema_cannot_express_is_blocked(
        self, agent: OptimizeAgent
    ) -> None:
        executor, adapter = build_tools()
        context = make_context(executor)
        context.campaign_refs[CAMP_A] = {
            **context.campaign_refs[CAMP_A],
            "platform": UNKNOWN_NETWORK,
        }
        action = proposal(ActionType.PAUSE_CAMPAIGN)

        records = await agent._preflight([action], context, 1)

        assert records[0]["outcome"] == "validation_failed"
        assert records[0]["refused"] is True
        assert records[0]["blocking"] is True
        # The executor reports the field it rejected, not the value: the point
        # is that the proposal names a network the schema does not know.
        assert "platform" in (records[0]["error"] or "")
        assert action["preflight"] == {
            "tool": "platform.pause_campaign",
            "outcome": "validation_failed",
            "refused": True,
            "blocking": True,
            "error": records[0]["error"],
        }
        assert adapter.calls == []

    async def test_a_blocked_rehearsal_is_counted_as_a_refusal(self, agent: OptimizeAgent) -> None:
        executor, _ = build_tools()
        context = make_context(executor)
        context.campaign_refs[CAMP_A] = {
            **context.campaign_refs[CAMP_A],
            "platform": UNKNOWN_NETWORK,
        }

        await agent._preflight([proposal(ActionType.PAUSE_CAMPAIGN)], context, 1)

        usage = executor.usage(RUN_ID)
        assert usage["refusals"] == 1
        assert usage["dry_runs"] == 0
        assert usage["by_outcome"] == {"validation_failed": 1}

    async def test_a_budget_that_is_not_a_number_is_never_sent(self, agent: OptimizeAgent) -> None:
        executor, _ = build_tools()
        context = make_context(executor)
        action = proposal(ActionType.ADJUST_BUDGET, after_value="increase it a lot")

        assert agent._preflight_call(action, context) is None
        assert await agent._preflight([action], context, 1) == []
        assert executor.usage(RUN_ID)["invocations"] == 0


class TestTheRehearsalBudget:
    """Rehearsing costs budget, so it is capped and it is not free for nothing."""

    def test_the_cap_leaves_room_for_the_rest_of_the_run(self) -> None:
        assert ToolSettings().max_calls_per_run > MAX_PREFLIGHTS_PER_ITERATION

    async def test_the_rehearsal_stops_at_the_per_iteration_cap(
        self, agent: OptimizeAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(optimize_module, "MAX_PREFLIGHTS_PER_ITERATION", 2)
        executor, _ = build_tools()
        campaign_ids = ["camp_" + str(index) for index in range(6)]
        context = make_context(executor)
        for campaign_id in campaign_ids:
            context.campaign_refs[campaign_id] = {
                "platform": Platform.MOCK.value,
                "external_id": "ext_" + campaign_id,
                "name": campaign_id,
                "status": "active",
            }

        update = await agent.run(
            alert_state(
                *[alert(AlertRule.HIGH_CPA, campaign_id) for campaign_id in campaign_ids],
                campaign_ids=campaign_ids,
            ),
            context,
        )

        assert len(update["optimization_actions"]) == 6
        assert len(update["tool_preflights"]) == 2
        rehearsed = sum(1 for action in update["optimization_actions"] if "preflight" in action)
        assert rehearsed == 2
        assert executor.usage(RUN_ID)["invocations"] == 2


class TestTheMessageReportsTheRehearsal:
    """The run timeline has to say whether the proposals were checked."""

    def test_the_message_counts_rehearsals_and_blocks(self, agent: OptimizeAgent) -> None:
        message = agent._build_message(
            [],
            [],
            1,
            [{"blocking": True}, {"blocking": False}, {"blocking": False}],
        )

        assert "Rehearsed 3 write proposal(s)" in message["content"]
        assert "1 came back blocked" in message["content"]
        assert message["preflights"] == {"attempted": 3, "blocked": 1}

    def test_a_run_without_rehearsals_does_not_claim_any(self, agent: OptimizeAgent) -> None:
        message = agent._build_message([], [], 1, [])

        assert "Rehearsed" not in message["content"]
        assert message["preflights"] == {"attempted": 0, "blocked": 0}

    async def test_a_real_run_reports_its_own_rehearsal(self, agent: OptimizeAgent) -> None:
        executor, _ = build_tools()
        context = make_context(executor)

        update = await agent.run(alert_state(alert(AlertRule.HIGH_CPA)), context)

        message = update["agent_messages"][-1]
        assert message["agent"] == "optimize"
        assert "Rehearsed 1 write proposal(s)" in message["content"]
        assert "0 came back blocked" in message["content"]
        assert message["preflights"] == {"attempted": 1, "blocked": 0}


def perf_snapshot(campaign_id: str, *, cost: float, revenue: float) -> PerformanceSnapshot:
    """A mature campaign, so confidence and scale weighting are both maxed out."""
    return PerformanceSnapshot(
        campaign_id=campaign_id,
        campaign_name=campaign_id,
        impressions=100_000,
        clicks=3_000,
        conversions=200,
        total_cost=cost,
        total_revenue=revenue,
    )


def bid_decision(campaign_id: str, multiplier: float) -> dict[str, Any]:
    """One bidding decision in the shape the bidding agent emits."""
    return {"campaign_id": campaign_id, "multiplier": multiplier}


class TestTheBudgetHoldOnlyGuardsARealContradiction:
    """A campaign on target is protected only when this run also bids it up.

    The guard exists so one run cannot propose "bid more here" and "spend less
    here" about the same campaign. Protecting every campaign that merely clears
    its target is far wider, and it backfires: on a portfolio running above
    target every lower bound gets pinned to its current budget, the allocator is
    left with no headroom, and the budget agent proposes nothing at all.
    """

    def test_on_target_with_no_bid_proposal_is_not_held(self, agent: OptimizeAgent) -> None:
        context = make_context(None, campaign_targets={CAMP_A: {"target_roas": 2.0}})

        held = agent._hold_at_target(
            [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)], [], context
        )

        assert held == set()

    def test_on_target_and_being_bid_up_is_held(self, agent: OptimizeAgent) -> None:
        context = make_context(None, campaign_targets={CAMP_A: {"target_roas": 2.0}})

        held = agent._hold_at_target(
            [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)],
            [bid_decision(CAMP_A, 1.3)],
            context,
        )

        assert held == {CAMP_A}

    def test_below_target_is_not_held_even_when_being_bid_up(self, agent: OptimizeAgent) -> None:
        """The bid agent would not raise this bid, so there is no contradiction."""
        context = make_context(None, campaign_targets={CAMP_A: {"target_roas": 20.0}})

        held = agent._hold_at_target(
            [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)],
            [bid_decision(CAMP_A, 1.3)],
            context,
        )

        assert held == set()

    def test_a_bid_cut_does_not_hold(self, agent: OptimizeAgent) -> None:
        """Both proposals spend less here, so they already agree."""
        context = make_context(None, campaign_targets={CAMP_A: {"target_roas": 2.0}})

        held = agent._hold_at_target(
            [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)],
            [bid_decision(CAMP_A, 0.6)],
            context,
        )

        assert held == set()

    def test_a_campaign_with_no_target_falls_back_to_the_default(
        self, agent: OptimizeAgent
    ) -> None:
        context = make_context(None)

        held = agent._hold_at_target(
            [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)],
            [bid_decision(CAMP_A, 1.3)],
            context,
        )

        assert held == {CAMP_A}

    def test_a_budget_is_held_exactly_when_a_bid_proposal_exists(
        self, agent: OptimizeAgent
    ) -> None:
        """The guard and the bid agent read one threshold, so they cannot drift.

        A hold that fires on a multiplier _bid_actions is about to drop as noise
        protects a budget for a proposal that does not exist.
        """
        context = make_context(None, campaign_targets={CAMP_A: {"target_roas": 2.0}})
        snapshots = [perf_snapshot(CAMP_A, cost=1_000.0, revenue=9_000.0)]

        for multiplier in (1.0, 1.0 + BID_MOVE_THRESHOLD - 0.001, 1.0 + BID_MOVE_THRESHOLD, 1.3):
            decisions = [bid_decision(CAMP_A, multiplier)]
            held = agent._hold_at_target(snapshots, decisions, context)
            proposed = bool(agent._bid_actions(decisions, iteration=1))
            assert held == ({CAMP_A} if proposed else set()), multiplier
