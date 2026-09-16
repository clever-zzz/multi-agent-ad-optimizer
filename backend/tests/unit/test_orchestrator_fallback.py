"""The sequential fallback has to be indistinguishable from the graph path.

``langgraph`` is a required dependency, so ``_invoke_sequential`` never runs in
development or in CI unless a test forces it. That is exactly why it needs these
tests: it is the only implementation of "the orchestrator still works without
LangGraph", and nothing in the suite executed it before this file existed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import adoptimizer
from adoptimizer.agents.base import AgentContext
from adoptimizer.core.config import LLMProvider, LLMSettings, OptimizationSettings
from adoptimizer.domain.enums import AgentName, RunStatus
from adoptimizer.llm.gateway import build_gateway
from adoptimizer.orchestrator.events import EventBus
from adoptimizer.orchestrator.graph import _REDUCERS, OptimizationOrchestrator
from adoptimizer.orchestrator.state import (
    UNWIRED_BY_AGENTS,
    AgentState,
    append_list,
    initial_state,
    max_int,
    merge_mapping,
    replace_list,
    summarise_state,
)

ALERT = {"rule": "high_cpa", "campaign_id": "camp_a", "severity": "warning"}

_AGENTS_DIR = Path(adoptimizer.__file__).parent / "agents"


def _round_of(state: AgentState) -> int:
    return int(state.get("iteration", 0) or 0)


async def _monitor_stub(state: AgentState, context: AgentContext) -> dict[str, Any]:
    """Advance the round, and keep one alert alive so the loop runs twice."""
    current = _round_of(state) + 1
    return {
        "iteration": current,
        "current_agent": AgentName.MONITOR.value,
        "metrics": [{"campaign_id": "camp_a", "round": current}],
        "alerts": [dict(ALERT)] if current < 2 else [],
        "agent_messages": [
            {"agent": "monitor", "content": "round " + str(current), "iteration": current}
        ],
    }


async def _optimize_stub(state: AgentState, context: AgentContext) -> dict[str, Any]:
    """One proposal per round, so a broken append reducer loses a whole round."""
    current = _round_of(state)
    return {
        "current_agent": AgentName.OPTIMIZE.value,
        "optimization_actions": [
            {
                "id": "act_" + str(current),
                "campaign_id": "camp_a",
                "action_type": "adjust_budget",
                "status": "proposed",
                "confidence": 0.6,
                "iteration": current,
                "reason": "stub proposal",
            }
        ],
    }


async def _critic_stub(state: AgentState, context: AgentContext) -> dict[str, Any]:
    current = _round_of(state)
    return {
        "current_agent": AgentName.CRITIC.value,
        "critic_findings": [{"kind": "stub", "iteration": current}],
    }


def _quiet_stub(name: str) -> Any:
    async def _run(state: AgentState, context: AgentContext) -> dict[str, Any]:
        return {"current_agent": name}

    return _run


@pytest.fixture
def context() -> AgentContext:
    return AgentContext(
        run_id="run_fallback",
        optimization=OptimizationSettings(use_convex_solver=False),
        gateway=build_gateway(LLMSettings(provider=LLMProvider.MOCK, cache_enabled=False)),
        bus=EventBus(),
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> OptimizationOrchestrator:
    """An orchestrator whose six agents do deterministic, accumulating work."""
    orchestrator = OptimizationOrchestrator()
    stubs = {
        AgentName.MONITOR: _monitor_stub,
        AgentName.OPTIMIZE: _optimize_stub,
        AgentName.CRITIC: _critic_stub,
    }
    for name, agent in orchestrator._ordered_agents():
        stub = stubs.get(name) or _quiet_stub(name.value)
        monkeypatch.setattr(agent, "run", stub)
    return orchestrator


class TestReducerTable:
    """The table is derived from ``AgentState`` now, so pin what it derives."""

    def test_it_matches_the_intended_merge_semantics(self) -> None:
        # This is a golden table on purpose. Deriving `_REDUCERS` from the
        # annotations removed the hand-mirrored copy, and the first thing the
        # derivation found was that the copy had already drifted: `usage` is
        # annotated `merge_mapping` on AgentState but was absent from the old
        # literal, so the fallback executor silently overwrote it instead of
        # merging. Pinning the result keeps both failure modes visible.
        expected = {
            "metrics": replace_list,
            "daily_budgets": merge_mapping,
            "audience_observations": replace_list,
            "audience_insights": merge_mapping,
            "health": merge_mapping,
            "new_creatives": append_list,
            "bidding_decisions": replace_list,
            "budget_allocations": replace_list,
            "optimization_actions": append_list,
            "critic_findings": append_list,
            "alerts": replace_list,
            "platform_checks": replace_list,
            "tool_preflights": append_list,
            "agent_messages": append_list,
            "iteration": max_int,
            "usage": merge_mapping,
        }
        assert expected == _REDUCERS

    def test_it_leaves_plain_channels_alone(self) -> None:
        # These carry no Annotated metadata. Inventing a reducer for one of them
        # would be a silent behaviour change on the fallback path only.
        for channel in (
            "run_id",
            "campaign_ids",
            "window_days",
            "max_iterations",
            "is_complete",
            "current_agent",
        ):
            assert channel not in _REDUCERS


def _channels_written_by_agents() -> set[str]:
    """State keys the agent modules actually write.

    A static scan rather than a fixture-driven run: the invariant is about the
    source, and any single run only exercises the branches its data happens to
    reach -- which is precisely how an unwritten channel survives a green suite.
    """
    written: set[str] = set()
    for path in sorted(_AGENTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                written.update(
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            elif isinstance(node, ast.Assign):
                written.update(
                    target.slice.value
                    for target in node.targets
                    if isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                )
    return written


class TestChannelWiring:
    """A channel nobody writes reads as empty forever, and that reads like a bug
    in the agent rather than a missing wire. Pin the difference."""

    def test_every_reducer_channel_is_wired_or_declared_unwired(self) -> None:
        missing = set(_REDUCERS) - _channels_written_by_agents() - set(UNWIRED_BY_AGENTS)
        assert not missing, (
            "these channels carry a reducer but no agent writes them, so they read "
            "as empty forever: " + ", ".join(sorted(missing))
        )

    def test_the_unwired_allowlist_has_no_stale_entries(self) -> None:
        stale = set(UNWIRED_BY_AGENTS) - set(_REDUCERS)
        assert not stale, "no longer a reducer channel: " + ", ".join(sorted(stale))

    def test_every_unwired_entry_carries_a_reason(self) -> None:
        for channel, reason in UNWIRED_BY_AGENTS.items():
            assert reason.strip(), channel + " needs a reason"

    def test_the_cross_iteration_alert_channel_is_gone(self) -> None:
        # Suppressing an alert whose fingerprint was seen in an earlier iteration
        # would empty ``alerts``, and ``_route_after_critic`` reads ``alerts`` to
        # decide whether to loop -- the run would stop after one iteration. The
        # channel was declared for a feature the control flow cannot support, so
        # it is removed rather than wired.
        assert "alert_fingerprints" not in _REDUCERS


class TestPathEquivalence:
    async def test_both_executors_produce_the_same_run(
        self, stubbed: OptimizationOrchestrator, context: AgentContext
    ) -> None:
        assert stubbed.execution_mode == "langgraph"
        via_graph = await stubbed.run(
            initial_state(max_iterations=2, run_id="run_fallback"), context
        )

        stubbed._graph = None
        assert stubbed.execution_mode == "sequential"
        via_loop = await stubbed.run(
            initial_state(max_iterations=2, run_id="run_fallback"), context
        )

        left = summarise_state(via_graph, status=RunStatus.SUCCEEDED)
        right = summarise_state(via_loop, status=RunStatus.SUCCEEDED)
        # Wall-clock is the one field that legitimately differs between two runs.
        left["usage"].pop("duration_s", None)
        right["usage"].pop("duration_s", None)
        assert left == right

    async def test_the_accumulating_channels_really_accumulate(
        self, stubbed: OptimizationOrchestrator, context: AgentContext
    ) -> None:
        """Guard against the comparison above passing on an empty run."""
        stubbed._graph = None
        summary = summarise_state(
            await stubbed.run(initial_state(max_iterations=2, run_id="run_fallback"), context),
            status=RunStatus.SUCCEEDED,
        )
        assert summary["iterations"] == 2
        assert summary["actions_proposed"] == 2
        assert summary["critic_findings"] == 2
        assert len(summary["messages"]) == 2
        # replace_list channels keep only the newest round.
        assert summary["campaigns"] == 1
        assert summary["alerts_raised"] == 0
