"""LangGraph state schema and reducers.

Two properties matter for correctness here and both were broken in the demo:

1. The schema must be an explicit TypedDict. Passing a bare ``dict`` to
   StateGraph on langgraph 1.x gives every node only the previous node's return
   value, so downstream agents silently see an empty state.
2. Accumulating channels need real reducers. The demo defined merge helpers and
   never wired them up, so multi-iteration runs overwrote earlier findings.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from ..core.ids import new_id
from ..domain.audience import SegmentObservation
from ..domain.enums import RunStatus
from ..domain.kpi import PerformanceSnapshot


def replace_list(_existing: list[Any] | None, incoming: list[Any] | None) -> list[Any]:
    """Last-value reducer that tolerates a missing update."""
    return list(incoming) if incoming is not None else (_existing or [])


def append_list(existing: list[Any] | None, incoming: list[Any] | None) -> list[Any]:
    """Accumulating reducer: findings from every iteration are preserved."""
    if incoming is None:
        return list(existing or [])
    return [*list(existing or []), *list(incoming)]


def merge_mapping(
    existing: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any]:
    """Shallow-merge reducer for dictionary channels."""
    merged = dict(existing or {})
    if incoming:
        merged.update(incoming)
    return merged


def max_int(existing: int | None, incoming: int | None) -> int:
    """Monotonic counter reducer so a replayed node cannot rewind progress."""
    return max(existing or 0, incoming or 0)


class AgentState(TypedDict, total=False):
    """Shared state flowing through the supervisor graph."""

    run_id: str
    campaign_ids: list[str]
    window_days: int
    max_iterations: int

    # Both shapes are legal on these two channels: the supervisor hands over
    # live model instances in-process, while a checkpointed or replayed run
    # restores plain dicts. Agents accept either.
    metrics: Annotated[list[dict[str, Any] | PerformanceSnapshot], replace_list]
    daily_budgets: Annotated[dict[str, float], merge_mapping]
    audience_observations: Annotated[list[dict[str, Any] | SegmentObservation], replace_list]
    audience_insights: Annotated[dict[str, Any], merge_mapping]
    health: Annotated[dict[str, Any], merge_mapping]

    new_creatives: Annotated[list[dict[str, Any]], append_list]
    bidding_decisions: Annotated[list[dict[str, Any]], replace_list]
    budget_allocations: Annotated[list[dict[str, Any]], replace_list]
    optimization_actions: Annotated[list[dict[str, Any]], append_list]
    critic_findings: Annotated[list[dict[str, Any]], append_list]

    alerts: Annotated[list[dict[str, Any]], replace_list]

    # Tool-layer evidence. Both channels are written by agents that called a
    # tool, and both are what makes "the agent checked with the network" a
    # inspectable fact rather than a claim in a log line.
    platform_checks: Annotated[list[dict[str, Any]], replace_list]
    tool_preflights: Annotated[list[dict[str, Any]], append_list]
    agent_messages: Annotated[list[dict[str, Any]], append_list]

    current_agent: str
    iteration: Annotated[int, max_int]
    is_complete: bool
    usage: Annotated[dict[str, Any], merge_mapping]


# Channels that carry a reducer but that no agent ever writes, with the reason
# each one is unwired. An empty value here is expected, not a wiring mistake.
#
# Listing them is what lets the guard test in
# ``tests/unit/test_orchestrator_fallback.py`` separate "seeded elsewhere on
# purpose" from "declared and then forgotten" -- the second failure mode is how
# a dropped ``usage`` merge hid on the fallback path for months, because the
# table was hand-mirrored and nothing compared it against the writers.
#
# A channel that appears in neither the agent write sites nor this mapping
# fails that test. Adding one here is a deliberate act; forgetting to wire a new
# channel is not.
UNWIRED_BY_AGENTS: dict[str, str] = {
    "daily_budgets": "seeded by the service from the campaign snapshot; agents read AgentContext",
    "audience_observations": "loaded by the service from the warehouse; agents read AgentContext",
    "usage": "folded in by the orchestrator from the gateway once the last node returns",
}


def initial_state(
    *,
    campaign_ids: list[str] | None = None,
    max_iterations: int = 3,
    window_days: int = 7,
    run_id: str | None = None,
) -> AgentState:
    """Build the state that seeds a new optimization run."""
    state: AgentState = {
        "run_id": run_id or new_id("run"),
        "campaign_ids": list(campaign_ids or []),
        "window_days": window_days,
        "max_iterations": max_iterations,
        "metrics": [],
        "daily_budgets": {},
        "audience_observations": [],
        "audience_insights": {},
        "health": {},
        "new_creatives": [],
        "bidding_decisions": [],
        "budget_allocations": [],
        "optimization_actions": [],
        "critic_findings": [],
        "alerts": [],
        "platform_checks": [],
        "tool_preflights": [],
        "agent_messages": [],
        "current_agent": "",
        "iteration": 0,
        "is_complete": False,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
    }
    return state


def suppressed_action_ids(state: AgentState) -> set[str]:
    """Action ids the critic withheld, collected from its findings.

    The isinstance guard is not decoration: a checkpointed or replayed run
    restores this channel from JSON, where nothing enforces the shape.
    """
    return {
        str(action_id)
        for finding in (state.get("critic_findings") or [])
        if isinstance(finding, dict)
        for action_id in (finding.get("suppressed_action_ids") or [])
    }


def surviving_actions(state: AgentState) -> list[dict[str, Any]]:
    """Proposals the critic let through, in proposal order.

    The critic marks rather than deletes, because `optimization_actions`
    accumulates and no node may rewrite history. Every consumer that persists or
    counts proposals has to go through this filter, otherwise a withheld action
    still reaches the approval queue and from there an ad platform.
    """
    dropped = suppressed_action_ids(state)
    actions = [
        action for action in (state.get("optimization_actions") or []) if isinstance(action, dict)
    ]
    if not dropped:
        return actions
    return [action for action in actions if str(action.get("id") or "") not in dropped]


def summarise_state(state: AgentState, *, status: RunStatus) -> dict[str, Any]:
    """Flatten the final state into the run summary stored in the database."""
    proposed = [
        action for action in (state.get("optimization_actions") or []) if isinstance(action, dict)
    ]
    actions = surviving_actions(state)
    findings = [
        finding for finding in (state.get("critic_findings") or []) if isinstance(finding, dict)
    ]
    allocations = list(state.get("budget_allocations") or [])
    alerts = list(state.get("alerts") or [])
    preflights = [item for item in (state.get("tool_preflights") or []) if isinstance(item, dict)]
    health = dict(state.get("health") or {})
    usage = dict(state.get("usage") or {})

    action_counts: dict[str, int] = {}
    for action in actions:
        key = str(action.get("action_type", "unknown"))
        action_counts[key] = action_counts.get(key, 0) + 1

    # Verdicts per kind, including the ones that escalated instead of
    # suppressing, so the summary says *why* the queue is shorter than the raw
    # proposal count rather than only how much shorter it is.
    findings_by_kind: dict[str, int] = {}
    for finding in findings:
        key = str(finding.get("kind", "unknown"))
        findings_by_kind[key] = findings_by_kind.get(key, 0) + 1

    return {
        "status": status.value,
        "iterations": int(state.get("iteration", 0) or 0),
        "campaigns": len(state.get("metrics") or []),
        "creatives_generated": len(state.get("new_creatives") or []),
        "bidding_decisions": len(state.get("bidding_decisions") or []),
        "budget_adjustments": len(allocations),
        "actions": len(actions),
        "actions_proposed": len(proposed),
        "actions_suppressed": len(proposed) - len(actions),
        "critic_findings": len(findings),
        "critic_findings_by_kind": findings_by_kind,
        "platform_checks": len(state.get("platform_checks") or []),
        "tool_preflights": len(preflights),
        "preflights_blocked": sum(1 for item in preflights if item.get("blocking")),
        "action_counts": action_counts,
        "alerts_raised": len(alerts),
        "health": health,
        "usage": usage,
        "messages": list(state.get("agent_messages") or []),
    }
