"""Supervisor graph: wires the agents into a loop with conditional routing.

Why the Supervisor pattern rather than a pipeline or a swarm:
- a pipeline cannot loop back after the optimizer finds new anomalies
- a swarm has no global view, so two agents can propose conflicting actions
- the supervisor owns routing, iteration limits and termination in one place

LangGraph is used when importable; otherwise an equivalent sequential executor
runs the same agents with the same reducers so behaviour is identical.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from ..agents.audience import AudienceAgent
from ..agents.base import AgentContext, BaseAgent, RunCancelled
from ..agents.bidding import BiddingAgent
from ..agents.creative import CreativeAgent
from ..agents.critic import CriticAgent
from ..agents.monitor import MonitorAgent
from ..agents.optimize import OptimizeAgent
from ..core.logging import get_logger
from ..core.metrics import ACTIVE_RUNS, AGENT_RUN_DURATION_SECONDS, AGENT_RUNS_TOTAL
from ..domain.enums import AgentName, RunStatus
from .state import (
    AgentState,
    append_list,
    max_int,
    merge_mapping,
    replace_list,
    summarise_state,
)

logger = get_logger(__name__)

CONTEXT_CONFIG_KEY = "__agent_context"

try:  # pragma: no cover - depends on the installed extras
    # RunnableConfig must be a real runtime import: LangGraph reads the parameter
    # annotation to decide whether a node accepts the config bag, and a node whose
    # `config` is typed as anything else is invoked with the state alone.
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph import END, StateGraph

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover
    HAS_LANGGRAPH = False

try:  # pragma: no cover
    from langgraph.checkpoint.memory import MemorySaver

    HAS_CHECKPOINTER = True
except ImportError:  # pragma: no cover
    HAS_CHECKPOINTER = False

# Reducer table mirrors the Annotated declarations on AgentState. It is used by
# the sequential executor so both paths merge updates identically.
_REDUCERS: dict[str, Any] = {
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
    "alert_fingerprints": append_list,
    "platform_checks": replace_list,
    "tool_preflights": append_list,
    "agent_messages": append_list,
    "iteration": max_int,
}


class OptimizationOrchestrator:
    """Owns the graph and the run lifecycle around it."""

    def __init__(
        self,
        *,
        monitor: MonitorAgent | None = None,
        audience: AudienceAgent | None = None,
        creative: CreativeAgent | None = None,
        bidding: BiddingAgent | None = None,
        optimize: OptimizeAgent | None = None,
        critic: CriticAgent | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.monitor = monitor or MonitorAgent()
        self.audience = audience or AudienceAgent()
        self.creative = creative or CreativeAgent()
        self.bidding = bidding or BiddingAgent()
        self.optimize = optimize or OptimizeAgent()
        self.critic = critic or CriticAgent()
        self._checkpointer = checkpointer
        self._graph = self._compile() if HAS_LANGGRAPH else None

    @property
    def execution_mode(self) -> str:
        """Report which executor will handle a run."""
        return "langgraph" if self._graph is not None else "sequential"

    def _ordered_agents(self) -> list[tuple[AgentName, BaseAgent]]:
        return [
            (AgentName.MONITOR, self.monitor),
            (AgentName.AUDIENCE, self.audience),
            (AgentName.CREATIVE, self.creative),
            (AgentName.BIDDING, self.bidding),
            (AgentName.OPTIMIZE, self.optimize),
            (AgentName.CRITIC, self.critic),
        ]

    def _node_for(self, agent: BaseAgent) -> Any:
        """Adapt an agent to the LangGraph node signature (state, config).

        The run-scoped AgentContext travels in the config bag rather than being
        baked into the compiled graph, so one graph instance serves every run.
        """

        async def _node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            configurable = config.get("configurable") or {}
            context = configurable.get(CONTEXT_CONFIG_KEY)
            if context is None:
                msg = "Agent context was not supplied in the graph config"
                raise RuntimeError(msg)
            await context.raise_if_cancelled()
            return await agent.execute(state, context)

        _node.__name__ = str(agent.name.value)
        return _node

    def _compile(self) -> Any:
        """Build and compile the LangGraph state machine."""
        graph = StateGraph(AgentState)

        for agent_name, agent in self._ordered_agents():
            graph.add_node(agent_name.value, self._node_for(agent))

        graph.set_entry_point(AgentName.MONITOR.value)
        graph.add_edge(AgentName.MONITOR.value, AgentName.AUDIENCE.value)
        graph.add_edge(AgentName.AUDIENCE.value, AgentName.CREATIVE.value)
        graph.add_edge(AgentName.CREATIVE.value, AgentName.BIDDING.value)
        graph.add_edge(AgentName.BIDDING.value, AgentName.OPTIMIZE.value)
        graph.add_edge(AgentName.OPTIMIZE.value, AgentName.CRITIC.value)
        graph.add_conditional_edges(
            AgentName.CRITIC.value,
            self._route_after_critic,
            {"continue": AgentName.MONITOR.value, "end": END},
        )

        return graph.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _route_after_critic(state: AgentState) -> Literal["continue", "end"]:
        """Loop while anomalies remain and the iteration budget is unspent.

        Routing reads the post-critic state, so the loop continues only when
        unreconciled anomalies survive rather than when any rule fired.
        """
        if state.get("is_complete"):
            return "end"
        iteration = int(state.get("iteration", 0) or 0)
        if iteration >= int(state.get("max_iterations", 3) or 3):
            return "end"
        return "continue" if state.get("alerts") else "end"

    async def run(self, state: AgentState, context: AgentContext) -> AgentState:
        """Execute a full optimization run and publish terminal events."""
        started = time.perf_counter()
        ACTIVE_RUNS.inc()
        await context.bus.publish(
            context.run_id,
            "run.started",
            agent="supervisor",
            payload={
                "mode": self.execution_mode,
                "campaign_ids": list(state.get("campaign_ids") or []),
                "max_iterations": int(state.get("max_iterations", 3) or 3),
                "window_days": int(state.get("window_days", 7) or 7),
            },
        )

        try:
            if self._graph is not None:
                final_state: AgentState = await self._invoke_graph(state, context)
            else:
                final_state = await self._invoke_sequential(state, context)
        except RunCancelled:
            elapsed = time.perf_counter() - started
            AGENT_RUNS_TOTAL.labels(status=RunStatus.CANCELLED.value).inc()
            AGENT_RUN_DURATION_SECONDS.observe(elapsed)
            logger.info(
                "run_cancelled_midflight",
                run_id=context.run_id,
                duration_s=round(elapsed, 3),
            )
            await context.bus.publish(
                context.run_id,
                "run.cancelled",
                agent="supervisor",
                payload={"duration_s": round(elapsed, 3)},
            )
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            AGENT_RUNS_TOTAL.labels(status=RunStatus.FAILED.value).inc()
            AGENT_RUN_DURATION_SECONDS.observe(elapsed)
            logger.error(
                "run_failed", run_id=context.run_id, error=str(exc), duration_s=round(elapsed, 3)
            )
            await context.bus.publish(
                context.run_id,
                "run.failed",
                agent="supervisor",
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        finally:
            ACTIVE_RUNS.dec()

        elapsed = time.perf_counter() - started
        status = RunStatus.SUCCEEDED
        AGENT_RUNS_TOTAL.labels(status=status.value).inc()
        AGENT_RUN_DURATION_SECONDS.observe(elapsed)

        # Folded in before the summary is built so the run.succeeded event, the
        # persisted summary and the run's token columns all report one set of
        # numbers. The gateway is the authority: it sees every completion,
        # including retries and fallbacks an agent never reports upward.
        final_state["usage"] = {
            **dict(final_state.get("usage") or {}),
            **context.gateway.usage(context.run_id),
            "duration_s": round(elapsed, 3),
        }

        summary = summarise_state(final_state, status=status)
        await context.bus.publish(
            context.run_id,
            "run.succeeded",
            agent="supervisor",
            payload={"duration_s": round(elapsed, 3), "summary": summary},
        )
        logger.info(
            "run_succeeded",
            run_id=context.run_id,
            duration_s=round(elapsed, 3),
            iterations=summary["iterations"],
            actions=summary["actions"],
        )

        return final_state

    async def _invoke_graph(self, state: AgentState, context: AgentContext) -> AgentState:
        """Run through LangGraph, injecting the context via the config bag."""
        assert self._graph is not None
        config: RunnableConfig = {
            "configurable": {"thread_id": context.run_id, CONTEXT_CONFIG_KEY: context},
            "recursion_limit": 6 * int(state.get("max_iterations", 3) or 3) + 8,
        }
        final: AgentState = await self._graph.ainvoke(state, config=config)
        return final

    async def _invoke_sequential(self, state: AgentState, context: AgentContext) -> AgentState:
        """Equivalent executor for environments without LangGraph installed."""
        current: AgentState = dict(state)  # type: ignore[assignment]
        max_iterations = int(current.get("max_iterations", 3) or 3)

        for _ in range(max_iterations):
            for _agent_name, agent in self._ordered_agents():
                await context.raise_if_cancelled()
                update = await agent.execute(current, context)
                current = _apply_update(current, update)

            if current.get("is_complete"):
                break
            if not current.get("alerts"):
                break
        return current


def _apply_update(state: AgentState, update: dict[str, Any]) -> AgentState:
    """Merge an agent update using the same reducers LangGraph would apply."""
    merged: AgentState = dict(state)  # type: ignore[assignment]
    for key, value in update.items():
        reducer = _REDUCERS.get(key)
        if reducer is None:
            merged[key] = value  # type: ignore[literal-required]
        else:
            merged[key] = reducer(state.get(key), value)  # type: ignore[literal-required]
    return merged


def build_orchestrator(*, checkpointer: Any = None) -> OptimizationOrchestrator:
    """Construct the orchestrator with default agents."""
    if checkpointer is None and HAS_CHECKPOINTER:
        checkpointer = MemorySaver()
    orchestrator = OptimizationOrchestrator(checkpointer=checkpointer)
    logger.info(
        "orchestrator_built",
        mode=orchestrator.execution_mode,
        checkpointing=checkpointer is not None,
    )
    return orchestrator
