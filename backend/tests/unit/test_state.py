"""Unit tests for the LangGraph state schema and its reducers.

Two demo bugs are guarded here: a bare ``dict`` schema starved downstream nodes,
and accumulating channels had no reducers so iteration 2 overwrote iteration 1.
"""

from __future__ import annotations

from typing import get_type_hints

from adoptimizer.domain.enums import RunStatus
from adoptimizer.orchestrator.state import (
    AgentState,
    append_list,
    initial_state,
    max_int,
    merge_mapping,
    replace_list,
    summarise_state,
)


class TestReplaceList:
    def test_incoming_wins(self) -> None:
        assert replace_list([1, 2], [3]) == [3]

    def test_none_update_keeps_existing(self) -> None:
        assert replace_list([1, 2], None) == [1, 2]

    def test_missing_existing_becomes_empty(self) -> None:
        assert replace_list(None, None) == []

    def test_returns_a_copy(self) -> None:
        incoming = [1, 2]
        result = replace_list(None, incoming)
        result.append(3)
        assert incoming == [1, 2]


class TestAppendList:
    def test_accumulates_across_iterations(self) -> None:
        """The core regression: findings from every iteration must survive."""
        first = append_list(None, ["a"])
        second = append_list(first, ["b"])
        third = append_list(second, ["c"])
        assert third == ["a", "b", "c"]

    def test_none_update_is_a_no_op(self) -> None:
        assert append_list(["a"], None) == ["a"]

    def test_does_not_mutate_the_existing_list(self) -> None:
        existing = ["a"]
        append_list(existing, ["b"])
        assert existing == ["a"]


class TestMergeMapping:
    def test_shallow_merge(self) -> None:
        assert merge_mapping({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_incoming_overrides(self) -> None:
        assert merge_mapping({"a": 1}, {"a": 2}) == {"a": 2}

    def test_none_inputs(self) -> None:
        assert merge_mapping(None, None) == {}
        assert merge_mapping({"a": 1}, None) == {"a": 1}


class TestMaxInt:
    def test_monotonic(self) -> None:
        assert max_int(3, 1) == 3
        assert max_int(1, 3) == 3

    def test_none_treated_as_zero(self) -> None:
        assert max_int(None, 2) == 2
        assert max_int(2, None) == 2
        assert max_int(None, None) == 0


class TestSchema:
    def test_schema_is_an_explicit_typed_dict(self) -> None:
        """Passing a bare dict to StateGraph gives nodes only the previous return."""
        assert getattr(AgentState, "__total__", None) is False
        assert isinstance(AgentState.__annotations__, dict)

    def test_accumulating_channels_are_annotated_with_a_reducer(self) -> None:
        hints = get_type_hints(AgentState, include_extras=True)
        for channel in (
            "new_creatives",
            "optimization_actions",
            "critic_findings",
            "agent_messages",
            "tool_preflights",
        ):
            metadata = getattr(hints[channel], "__metadata__", ())
            assert append_list in metadata, channel + " must accumulate"

    def test_last_value_channels_use_replace(self) -> None:
        hints = get_type_hints(AgentState, include_extras=True)
        for channel in ("metrics", "bidding_decisions", "budget_allocations", "alerts"):
            metadata = getattr(hints[channel], "__metadata__", ())
            assert replace_list in metadata, channel + " must be last-value"

    def test_iteration_uses_the_monotonic_reducer(self) -> None:
        hints = get_type_hints(AgentState, include_extras=True)
        assert max_int in getattr(hints["iteration"], "__metadata__", ())

    def test_mapping_channels_merge(self) -> None:
        hints = get_type_hints(AgentState, include_extras=True)
        for channel in ("daily_budgets", "audience_insights", "health", "usage"):
            assert merge_mapping in getattr(hints[channel], "__metadata__", ())


class TestInitialState:
    def test_defaults(self) -> None:
        state = initial_state()
        assert state["run_id"].startswith("run")
        assert state["campaign_ids"] == []
        assert state["iteration"] == 0
        assert state["is_complete"] is False
        assert state["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

    def test_explicit_run_id_and_campaigns(self) -> None:
        state = initial_state(
            campaign_ids=["a", "b"], max_iterations=5, window_days=14, run_id="run_x"
        )
        assert state["run_id"] == "run_x"
        assert state["campaign_ids"] == ["a", "b"]
        assert state["max_iterations"] == 5
        assert state["window_days"] == 14

    def test_campaign_list_is_copied(self) -> None:
        source = ["a"]
        state = initial_state(campaign_ids=source)
        source.append("b")
        assert state["campaign_ids"] == ["a"]

    def test_every_annotated_channel_is_initialised(self) -> None:
        state = initial_state()
        for channel in AgentState.__annotations__:
            assert channel in state, channel + " is missing from initial_state"


class TestSummariseState:
    def test_counts_actions_by_type(self) -> None:
        state = initial_state()
        state["optimization_actions"] = [
            {"action_type": "adjust_budget"},
            {"action_type": "adjust_budget"},
            {"action_type": "pause_creative"},
        ]
        summary = summarise_state(state, status=RunStatus.SUCCEEDED)
        assert summary["actions"] == 3
        assert summary["action_counts"] == {"adjust_budget": 2, "pause_creative": 1}
        assert summary["status"] == "succeeded"

    def test_carries_iterations_and_usage(self) -> None:
        state = initial_state()
        state["iteration"] = 2
        state["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.01}
        summary = summarise_state(state, status=RunStatus.FAILED)
        assert summary["iterations"] == 2
        assert summary["usage"]["cost_usd"] == 0.01
        assert summary["status"] == "failed"

    def test_empty_state_is_safe(self) -> None:
        summary = summarise_state(AgentState(), status=RunStatus.CANCELLED)  # type: ignore[typeddict-item]
        assert summary["actions"] == 0
        assert summary["campaigns"] == 0
        assert summary["messages"] == []
        assert summary["status"] == "cancelled"

    def test_messages_are_preserved_for_the_timeline(self) -> None:
        state = initial_state()
        state["agent_messages"] = [{"agent": "monitor", "text": "ok"}]
        summary = summarise_state(state, status=RunStatus.SUCCEEDED)
        assert summary["messages"] == [{"agent": "monitor", "text": "ok"}]

    def test_a_run_that_never_reached_the_critic_reports_no_verdicts(self) -> None:
        """A failed or cancelled run is summarised from the initial state.

        ``execute_run`` only rebinds its ``state`` when the orchestrator returns
        normally, so a run that raised keeps the initial state and its summary
        carries zeroed counters. That is what stops a persisted summary from
        claiming critic verdicts - or proposals - that no row can back, which
        would reintroduce the bare-count-with-no-detail problem the findings
        table exists to solve.
        """
        summary = summarise_state(initial_state(), status=RunStatus.FAILED)

        assert summary["critic_findings"] == 0
        assert summary["critic_findings_by_kind"] == {}
        assert summary["actions"] == 0
        assert summary["actions_proposed"] == 0
        assert summary["actions_suppressed"] == 0
