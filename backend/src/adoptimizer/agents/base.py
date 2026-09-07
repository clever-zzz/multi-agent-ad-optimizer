"""Agent execution contract.

Every agent runs through the same template method so timing, metrics, event
publication and error handling are implemented once rather than five times.
Agents are pure with respect to I/O: they receive an AgentContext and return a
partial state update.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.config import OptimizationSettings
from ..core.logging import get_logger, run_id_ctx
from ..core.metrics import AGENT_STEP_DURATION_SECONDS, AGENT_STEP_TOTAL
from ..domain.enums import AgentName
from ..llm.gateway import LLMGateway
from ..orchestrator.events import EventBus
from ..orchestrator.state import AgentState

logger = get_logger(__name__)

# Returns True once the run has been closed out of band, for example by an
# operator cancelling it from another request or another API replica.
CancellationCheck = Callable[[], Awaitable[bool]]


class RunCancelled(Exception):
    """Raised between agent steps when the run was cancelled externally."""

    def __init__(self, run_id: str) -> None:
        super().__init__("Run " + run_id + " was cancelled")
        self.run_id = run_id


@dataclass(slots=True)
class AgentContext:
    """Everything an agent may touch during a run."""

    run_id: str
    optimization: OptimizationSettings
    gateway: LLMGateway
    bus: EventBus
    actor: str = "system"
    warehouse: Any = None
    platforms: Any = None
    snapshots: list[Any] = field(default_factory=list)
    audience_observations: list[Any] = field(default_factory=list)
    existing_creatives: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    daily_budgets: dict[str, float] = field(default_factory=dict)
    campaign_targets: dict[str, dict[str, float]] = field(default_factory=dict)
    cancellation_check: CancellationCheck | None = None

    async def raise_if_cancelled(self) -> None:
        """Abort the run when it was closed while this step was pending.

        Cancellation is cooperative and checked between agent steps. Without it
        a run cancelled from another API replica would keep spending model
        tokens and then overwrite the cancelled status with its own result.
        """
        if self.cancellation_check is None:
            return
        if await self.cancellation_check():
            raise RunCancelled(self.run_id)


class BaseAgent(ABC):
    """Template for a single agent step."""

    name: AgentName = AgentName.MONITOR

    async def execute(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        """Run the agent, publishing lifecycle events and recording metrics."""
        started = time.perf_counter()
        run_id_ctx.set(context.run_id)

        await context.bus.publish(
            context.run_id,
            "agent.started",
            agent=self.name.value,
            payload={"iteration": int(state.get("iteration", 0) or 0)},
        )

        try:
            update = await self.run(state, context)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            AGENT_STEP_TOTAL.labels(agent=self.name.value, outcome="error").inc()
            AGENT_STEP_DURATION_SECONDS.labels(agent=self.name.value).observe(elapsed)
            logger.error(
                "agent_failed",
                agent=self.name.value,
                run_id=context.run_id,
                error=str(exc),
                duration_ms=round(elapsed * 1000, 2),
                exc_info=True,
            )
            await context.bus.publish(
                context.run_id,
                "agent.failed",
                agent=self.name.value,
                payload={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise

        elapsed = time.perf_counter() - started
        AGENT_STEP_TOTAL.labels(agent=self.name.value, outcome="success").inc()
        AGENT_STEP_DURATION_SECONDS.labels(agent=self.name.value).observe(elapsed)

        logger.info(
            "agent_completed",
            agent=self.name.value,
            run_id=context.run_id,
            duration_ms=round(elapsed * 1000, 2),
            updated_keys=sorted(update.keys()),
        )
        # The narrative lines are what an operator actually reads on the run
        # timeline, so they travel with the event instead of only living in the
        # accumulated state.
        narrative = [
            str(item.get("content"))
            for item in (update.get("agent_messages") or [])
            if isinstance(item, dict) and item.get("content")
        ]
        await context.bus.publish(
            context.run_id,
            "agent.completed",
            agent=self.name.value,
            payload={
                "duration_ms": round(elapsed * 1000, 2),
                "iteration": int(state.get("iteration", 0) or 0),
                "updated_keys": sorted(update.keys()),
                "summary": update.get("_summary", {}),
                "messages": narrative,
            },
        )

        update.pop("_summary", None)
        return update

    @abstractmethod
    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        """Produce the state update for this step."""

    def _message(
        self, content: str, *, iteration: int, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build a narrative message for the run timeline."""
        message: dict[str, Any] = {
            "agent": self.name.value,
            "content": content,
            "iteration": iteration,
        }
        if extra:
            message.update(extra)
        return message

    async def _accumulate_usage(self, context: AgentContext, result: Any) -> dict[str, Any]:
        """Fold one completion's token usage into the run totals."""
        usage = result.usage
        return {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cost_usd": usage.cost_usd,
            "calls": 1,
        }
