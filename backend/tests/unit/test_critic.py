"""Unit tests for the critic agent and the suppression helpers it relies on.

The critic is the only step that owns a global view of the proposal set, so the
invariants that matter are narrow: a contradiction never survives, the winner is
chosen by confidence with a conservative tie-break, and every suppression leaves
an auditable finding behind rather than silently dropping a proposal.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from adoptimizer.agents.base import AgentContext
from adoptimizer.agents.critic import CriticAgent
from adoptimizer.core.config import LLMProvider, LLMSettings, OptimizationSettings
from adoptimizer.domain.enums import ActionType, AgentName, RunStatus
from adoptimizer.llm.gateway import build_gateway
from adoptimizer.orchestrator.events import EventBus
from adoptimizer.orchestrator.graph import _REDUCERS, OptimizationOrchestrator
from adoptimizer.orchestrator.state import (
    AgentState,
    append_list,
    initial_state,
    summarise_state,
    suppressed_action_ids,
    surviving_actions,
)

_ids = itertools.count(1)

SPEND = {ActionType.ADJUST_BUDGET, ActionType.ADJUST_BID}


def action(
    action_type: ActionType,
    campaign_id: str = "camp_a",
    *,
    creative_id: str | None = None,
    confidence: float = 0.5,
    iteration: int = 1,
    reason: str = "proposed by a test",
    severity: str = "",
) -> dict[str, Any]:
    """Build one proposal in the shape the optimizer emits."""
    return {
        "id": "act_" + format(next(_ids), "05d"),
        "campaign_id": campaign_id,
        "creative_id": creative_id,
        "action_type": action_type.value,
        "status": "proposed",
        "confidence": confidence,
        "iteration": iteration,
        "reason": reason,
        "severity": severity,
    }


def state_with(proposals: list[dict[str, Any]], iteration: int = 1) -> AgentState:
    state = initial_state(run_id="run_critic", max_iterations=2)
    state["optimization_actions"] = proposals
    state["iteration"] = iteration
    return state


def merged(state: AgentState, update: dict[str, Any]) -> AgentState:
    """Apply a critic update the way the graph reducer would."""
    result = dict(state)  # type: ignore[assignment]
    for key, value in update.items():
        if key == "_summary":
            continue
        result[key] = append_list(state.get(key), value) if key in _REDUCERS else value  # type: ignore[literal-required]
    return result  # type: ignore[return-value]


@pytest.fixture
def context() -> AgentContext:
    return AgentContext(
        run_id="run_critic",
        optimization=OptimizationSettings(use_convex_solver=False),
        gateway=build_gateway(LLMSettings(provider=LLMProvider.MOCK, cache_enabled=False)),
        bus=EventBus(),
    )


@pytest.fixture
def critic() -> CriticAgent:
    return CriticAgent()


class TestCleanSets:
    async def test_an_empty_proposal_set_is_a_no_op(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        update = await critic.run(state_with([]), context)
        assert update["critic_findings"] == []
        assert update["current_agent"] == AgentName.CRITIC.value
        assert update["_summary"]["proposals"] == 0
        assert update["_summary"]["surviving"] == 0

    async def test_compatible_proposals_all_survive(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        proposals = [
            action(ActionType.ADJUST_BUDGET, "camp_a", confidence=0.75),
            action(ActionType.ADJUST_BID, "camp_a", confidence=0.60),
            action(ActionType.PAUSE_CREATIVE, "camp_b", creative_id="cre_1", confidence=0.80),
            action(ActionType.START_AB_TEST, "camp_c", confidence=0.70),
        ]
        state = state_with(proposals)
        update = await critic.run(state, context)

        assert update["critic_findings"] == []
        assert update["_summary"]["surviving"] == 4
        assert len(surviving_actions(merged(state, update))) == 4

    async def test_budget_and_bid_do_not_conflict_with_each_other(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """Tuning spend is one coherent intent even though it is two actions."""
        state = state_with(
            [
                action(ActionType.ADJUST_BUDGET, "camp_a", confidence=0.75),
                action(ActionType.ADJUST_BID, "camp_a", confidence=0.60),
            ]
        )
        update = await critic.run(state, context)
        assert update["_summary"]["suppressed"] == 0


class TestPauseVersusSpend:
    async def test_a_confident_pause_withholds_the_spend_changes(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90)
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        bid = action(ActionType.ADJUST_BID, confidence=0.60)
        state = state_with([pause, budget, bid])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert update["_summary"]["suppressed"] == 2

    async def test_a_stronger_bid_beats_the_pause(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """The critic is not hardcoded to pause: stronger evidence wins."""
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.70)
        bid = action(ActionType.ADJUST_BID, confidence=0.95)
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        state = state_with([pause, bid, budget])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert pause["id"] not in {a["id"] for a in survivors}
        assert {bid["id"], budget["id"]} == {a["id"] for a in survivors}

    async def test_equal_confidence_resolves_toward_stopping_spend(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.75)
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        state = state_with([budget, pause])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [pause["id"]]

    async def test_other_campaigns_are_not_collateral_damage(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        conflicted = action(ActionType.PAUSE_CAMPAIGN, "camp_a", confidence=0.90)
        conflicted_spend = action(ActionType.ADJUST_BUDGET, "camp_a", confidence=0.75)
        untouched = action(ActionType.ADJUST_BUDGET, "camp_b", confidence=0.75)
        state = state_with([conflicted, conflicted_spend, untouched])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert {a["id"] for a in survivors} == {conflicted["id"], untouched["id"]}


class TestCampaignLifecycleConflicts:
    async def test_pause_and_resume_cannot_both_survive(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90)
        resume = action(ActionType.RESUME_CAMPAIGN, confidence=0.50)
        state = state_with([pause, resume])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert update["critic_findings"][0]["kind"] == "campaign_pause_resume_conflict"

    async def test_an_experiment_on_a_paused_campaign_is_withheld(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """There is nothing to measure on a campaign that will not deliver."""
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90)
        experiment = action(ActionType.START_AB_TEST, confidence=0.70)
        state = state_with([pause, experiment])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert update["critic_findings"][0]["kind"] == "experiment_on_paused_campaign"


class TestCreativeConflicts:
    async def test_pause_and_resume_of_one_creative(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.80)
        resume = action(ActionType.RESUME_CREATIVE, creative_id="cre_1", confidence=0.40)
        state = state_with([pause, resume])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert update["critic_findings"][0]["scope"] == "creative"

    async def test_a_campaign_scoped_refresh_does_not_conflict_with_a_creative_pause(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """Different granularity: refresh carries no creative_id, so it stays."""
        pause = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.80)
        refresh = action(ActionType.REFRESH_CREATIVE, creative_id=None, confidence=0.90)
        state = state_with([pause, refresh])

        update = await critic.run(state, context)

        assert update["_summary"]["suppressed"] == 0
        assert len(surviving_actions(merged(state, update))) == 2

    async def test_two_creatives_of_one_campaign_are_independent(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        first = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.80)
        second = action(ActionType.RESUME_CREATIVE, creative_id="cre_2", confidence=0.60)
        state = state_with([first, second])

        update = await critic.run(state, context)

        assert update["_summary"]["suppressed"] == 0


class TestDuplicateProposals:
    async def test_the_same_intent_raised_every_iteration_is_decided_once(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """The actions channel accumulates, so iteration 2 repeats iteration 1."""
        first = action(ActionType.ADJUST_BUDGET, confidence=0.75, iteration=1)
        second = action(ActionType.ADJUST_BUDGET, confidence=0.75, iteration=2)
        state = state_with([first, second], iteration=2)

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert len(survivors) == 1
        assert update["critic_findings"][0]["kind"] == "duplicate_proposal"

    async def test_the_strongest_copy_is_the_one_kept(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        weak = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.45)
        strong = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.80)
        state = state_with([weak, strong], iteration=2)

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [strong["id"]]

    async def test_the_freshest_iteration_breaks_a_confidence_tie(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        stale = action(ActionType.ADJUST_BID, confidence=0.60, iteration=1)
        fresh = action(ActionType.ADJUST_BID, confidence=0.60, iteration=2)
        state = state_with([stale, fresh], iteration=2)

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [fresh["id"]]

    async def test_the_same_type_on_different_creatives_is_not_a_duplicate(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        one = action(ActionType.PAUSE_CREATIVE, creative_id="cre_1", confidence=0.80)
        two = action(ActionType.PAUSE_CREATIVE, creative_id="cre_2", confidence=0.80)
        state = state_with([one, two])

        update = await critic.run(state, context)

        assert update["_summary"]["suppressed"] == 0


class TestAuditability:
    async def test_every_suppression_names_the_loser_and_explains_itself(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90, reason="burn_rate alert")
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75, reason="reallocating spend")
        state = state_with([pause, budget])

        finding = (await critic.run(state, context))["critic_findings"][0]

        assert finding["kept_action_id"] == pause["id"]
        assert finding["suppressed_action_ids"] == [budget["id"]]
        assert finding["suppressed_actions"][0]["reason"] == "reallocating spend"
        assert "cannot both be applied" in finding["reason"]
        assert finding["kind"] == "pause_overrides_spend"

    async def test_suppression_marks_rather_than_deletes(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """An operator must still be able to see and overrule a withheld proposal."""
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90)
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        state = state_with([pause, budget])

        after = merged(state, await critic.run(state, context))

        assert len(after["optimization_actions"]) == 2
        assert suppressed_action_ids(after) == {budget["id"]}
        assert len(surviving_actions(after)) == 1

    async def test_the_narrative_reports_what_an_operator_will_see(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        state = state_with(
            [
                action(ActionType.PAUSE_CAMPAIGN, confidence=0.90),
                action(ActionType.ADJUST_BUDGET, confidence=0.75),
                action(ActionType.ADJUST_BID, confidence=0.60),
            ]
        )
        update = await critic.run(state, context)
        message = update["agent_messages"][0]

        assert message["agent"] == "critic"
        assert "withheld 2" in message["content"]
        assert "1 remain for approval" in message["content"]
        assert update["_summary"]["by_kind"] == {"pause_overrides_spend": 2}

    async def test_a_clean_set_says_so(self, critic: CriticAgent, context: AgentContext) -> None:
        state = state_with([action(ActionType.ADJUST_BID, confidence=0.6)])
        message = (await critic.run(state, context))["agent_messages"][0]
        assert "no conflicts found" in message["content"]


class TestExecuteTemplate:
    async def test_execute_publishes_lifecycle_events_and_drops_the_summary(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        state = state_with(
            [
                action(ActionType.PAUSE_CAMPAIGN, confidence=0.90),
                action(ActionType.ADJUST_BUDGET, confidence=0.75),
            ]
        )

        update = await critic.execute(state, context)
        events = context.bus.replay("run_critic")

        assert "_summary" not in update
        assert [event.event_type for event in events] == ["agent.started", "agent.completed"]
        assert all(event.agent == "critic" for event in events)
        assert events[1].payload["summary"]["suppressed"] == 1
        assert events[1].payload["messages"]

    async def test_non_dict_entries_in_the_channel_are_ignored(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """A replayed run can restore junk; the critic must not fall over."""
        state = state_with([action(ActionType.ADJUST_BID, confidence=0.6)])
        state["optimization_actions"] = [*state["optimization_actions"], "not-an-action"]

        update = await critic.run(state, context)

        assert update["_summary"]["proposals"] == 1


class TestSeverityOutranksEvidence:
    """Confidence is not one scale, so severity has to break the tie.

    `recommend_bid` scores evidence volume and reaches ~0.98 for any campaign
    over 50k impressions, while an alert-derived proposal scores anomaly
    severity (0.9 critical, 0.7 warning). Ranking on confidence alone let a
    well-delivered campaign talk its way out of a critical burn-rate pause.
    """

    async def test_a_critical_anomaly_beats_a_confident_bid(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(
            ActionType.PAUSE_CAMPAIGN,
            confidence=0.90,
            severity="critical",
            reason="Alert burn_rate: spending 3x the daily budget",
        )
        bid = action(ActionType.ADJUST_BID, confidence=0.98)
        budget = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        state = state_with([pause, bid, budget])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert update["critic_findings"][0]["kind"] == "pause_overrides_spend"
        assert "critical alert" in update["critic_findings"][0]["reason"]

    async def test_a_warning_anomaly_loses_to_strong_evidence(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """Severity only dominates when it is critical; weak alerts still lose."""
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.70, severity="warning")
        bid = action(ActionType.ADJUST_BID, confidence=0.98)
        state = state_with([pause, bid])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [bid["id"]]

    async def test_two_critical_anomalies_fall_back_to_confidence(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90, severity="critical")
        resume = action(ActionType.RESUME_CAMPAIGN, confidence=0.95, severity="critical")
        state = state_with([pause, resume])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [resume["id"]]

    async def test_severity_decides_which_duplicate_is_kept(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        plain = action(ActionType.ADJUST_BID, confidence=0.98, iteration=1)
        critical = action(ActionType.ADJUST_BID, confidence=0.70, severity="critical", iteration=2)
        state = state_with([plain, critical], iteration=2)

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [critical["id"]]

    async def test_the_withheld_record_carries_the_severity(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90, severity="critical")
        bid = action(ActionType.ADJUST_BID, confidence=0.98)
        state = state_with([pause, bid])

        finding = (await critic.run(state, context))["critic_findings"][0]

        assert finding["suppressed_actions"][0]["severity"] == ""
        assert finding["suppressed_actions"][0]["confidence"] == 0.98


class TestUnexecutableProposals:
    """A proposal the platform already refused never reaches the approval queue."""

    @staticmethod
    def rehearsed(
        proposal: dict[str, Any],
        *,
        outcome: str = "validation_failed",
        blocking: bool = True,
        tool: str = "platform.set_daily_budget",
        error: str = "daily_budget must be greater than 0",
    ) -> dict[str, Any]:
        """Attach the verdict the optimizer's dry run would have attached."""
        proposal["preflight"] = {
            "tool": tool,
            "outcome": outcome,
            "refused": True,
            "blocking": blocking,
            "error": error,
        }
        return proposal

    async def test_a_refused_proposal_is_withheld(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        blocked = self.rehearsed(action(ActionType.ADJUST_BUDGET))
        state = state_with([blocked])

        update = await critic.run(state, context)

        assert surviving_actions(merged(state, update)) == []
        finding = update["critic_findings"][0]
        assert finding["kind"] == "unexecutable_proposal"
        assert finding["kept_action_id"] == ""
        assert finding["kept_action_type"] == "none"
        assert finding["kept_confidence"] == 0.0
        assert update["_summary"]["by_kind"] == {"unexecutable_proposal": 1}

    async def test_the_reason_keeps_the_networks_own_words(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """An operator overruling this needs the refusal, not a paraphrase of it."""
        blocked = self.rehearsed(action(ActionType.PAUSE_CAMPAIGN), tool="platform.pause_campaign")
        state = state_with([blocked])

        finding = (await critic.run(state, context))["critic_findings"][0]

        assert "platform.pause_campaign" in finding["reason"]
        assert "daily_budget must be greater than 0" in finding["reason"]
        assert finding["scope"] == "campaign"

    async def test_a_creative_refusal_is_scoped_to_the_creative(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        blocked = self.rehearsed(
            action(ActionType.PAUSE_CREATIVE, creative_id="cre_1"),
            tool="platform.pause_creative",
        )
        state = state_with([blocked])

        finding = (await critic.run(state, context))["critic_findings"][0]

        assert finding["scope"] == "creative"
        assert finding["creative_id"] == "cre_1"

    async def test_the_withheld_record_carries_the_verdict(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        blocked = self.rehearsed(action(ActionType.ADJUST_BUDGET))
        state = state_with([blocked])

        withheld = (await critic.run(state, context))["critic_findings"][0]["suppressed_actions"][0]

        assert withheld["preflight"] is not None
        assert withheld["preflight"]["outcome"] == "validation_failed"
        assert withheld["preflight"]["blocking"] is True

    async def test_a_refusal_cannot_suppress_a_workable_alternative(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """Ordering is the point.

        The blocked pause must not take the budget change down with it, because a
        proposal the network would reject is not a reason to withhold one it
        would accept.
        """
        blocked_pause = self.rehearsed(
            action(ActionType.PAUSE_CAMPAIGN, confidence=0.90, severity="critical"),
            tool="platform.pause_campaign",
        )
        workable = action(ActionType.ADJUST_BUDGET, confidence=0.75)
        state = state_with([blocked_pause, workable])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [workable["id"]]

    async def test_the_unblocked_copy_of_a_repeated_intent_is_the_one_kept(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        blocked = self.rehearsed(action(ActionType.ADJUST_BUDGET, confidence=0.9, iteration=1))
        clean = action(ActionType.ADJUST_BUDGET, confidence=0.9, iteration=2)
        state = state_with([blocked, clean], iteration=2)

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [clean["id"]]

    async def test_a_deployment_state_refusal_still_reaches_the_operator(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """An exhausted budget is a capacity problem, not a defect in the proposal.

        Suppressing a sound change because the run ran out of tool calls would
        hide real work behind an infrastructure limit nobody can see.
        """
        starved = self.rehearsed(
            action(ActionType.ADJUST_BUDGET),
            outcome="budget_exhausted",
            blocking=False,
            error="run run_critic reached its tool budget of 500",
        )
        state = state_with([starved])

        update = await critic.run(state, context)

        assert [a["id"] for a in surviving_actions(merged(state, update))] == [starved["id"]]
        assert update["critic_findings"] == []

    async def test_a_clean_rehearsal_is_untouched(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        proposal = action(ActionType.ADJUST_BUDGET)
        proposal["preflight"] = {
            "tool": "platform.set_daily_budget",
            "outcome": "dry_run",
            "refused": False,
            "blocking": False,
            "error": None,
        }
        state = state_with([proposal])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [proposal["id"]]

    async def test_a_proposal_that_was_never_rehearsed_is_untouched(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """Advisory actions have no tool to call, and that is not a refusal."""
        proposal = action(ActionType.REFRESH_CREATIVE)
        state = state_with([proposal])

        survivors = surviving_actions(merged(state, await critic.run(state, context)))

        assert [a["id"] for a in survivors] == [proposal["id"]]

    async def test_a_corrupt_preflight_is_ignored_not_fatal(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        proposal = action(ActionType.ADJUST_BUDGET)
        proposal["preflight"] = "not-a-dict"
        state = state_with([proposal])

        assert surviving_actions(merged(state, await critic.run(state, context))) == [proposal]


class TestDefensiveParsing:
    async def test_a_corrupt_proposal_is_scored_as_zero_not_fatal(
        self, critic: CriticAgent, context: AgentContext
    ) -> None:
        """A replayed or hand-edited proposal must not take the run down."""
        pause = action(ActionType.PAUSE_CAMPAIGN, confidence=0.90)
        broken = action(ActionType.ADJUST_BUDGET)
        broken["confidence"] = "not-a-number"
        broken["iteration"] = "also-not-a-number"
        state = state_with([pause, broken])

        update = await critic.run(state, context)
        survivors = surviving_actions(merged(state, update))
        withheld = update["critic_findings"][0]["suppressed_actions"][0]

        assert [a["id"] for a in survivors] == [pause["id"]]
        assert withheld["confidence"] == 0.0
        assert withheld["iteration"] == 0


class TestStateHelpers:
    def test_surviving_actions_preserve_proposal_order(self) -> None:
        state = initial_state()
        first = action(ActionType.ADJUST_BID, "camp_a")
        second = action(ActionType.ADJUST_BID, "camp_b")
        state["optimization_actions"] = [first, second]
        state["critic_findings"] = [
            {"kind": "duplicate_proposal", "suppressed_action_ids": [first["id"]]}
        ]

        assert [a["id"] for a in surviving_actions(state)] == [second["id"]]

    def test_malformed_findings_cannot_break_the_filter(self) -> None:
        state = initial_state()
        proposal = action(ActionType.ADJUST_BID)
        state["optimization_actions"] = [proposal]
        state["critic_findings"] = ["junk", {"kind": "x"}, {"suppressed_action_ids": None}]  # type: ignore[list-item]

        assert suppressed_action_ids(state) == set()
        assert surviving_actions(state) == [proposal]

    def test_the_summary_reports_both_the_raw_and_the_reviewed_count(self) -> None:
        state = initial_state()
        kept = action(ActionType.PAUSE_CAMPAIGN)
        dropped = action(ActionType.ADJUST_BUDGET)
        state["optimization_actions"] = [kept, dropped]
        state["critic_findings"] = [
            {"kind": "pause_overrides_spend", "suppressed_action_ids": [dropped["id"]]}
        ]

        summary = summarise_state(state, status=RunStatus.SUCCEEDED)

        assert summary["actions"] == 1
        assert summary["actions_proposed"] == 2
        assert summary["actions_suppressed"] == 1
        assert summary["critic_findings"] == 1
        assert summary["action_counts"] == {"pause_campaign": 1}

    def test_a_run_without_a_critic_verdict_is_unchanged(self) -> None:
        state = initial_state()
        state["optimization_actions"] = [action(ActionType.ADJUST_BID)]

        summary = summarise_state(state, status=RunStatus.SUCCEEDED)

        assert summary["actions"] == 1
        assert summary["actions_suppressed"] == 0


class TestGraphWiring:
    def test_the_orchestrator_owns_a_critic(self) -> None:
        assert isinstance(OptimizationOrchestrator().critic, CriticAgent)

    def test_the_critic_runs_last_so_it_sees_every_proposal(self) -> None:
        ordered = [name for name, _agent in OptimizationOrchestrator()._ordered_agents()]
        assert ordered[-1] == AgentName.CRITIC
        assert ordered[ordered.index(AgentName.OPTIMIZE) + 1] == AgentName.CRITIC

    def test_findings_accumulate_like_the_other_verdict_channels(self) -> None:
        assert _REDUCERS["critic_findings"] is append_list
