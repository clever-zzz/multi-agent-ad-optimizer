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
from ..tools.executor import ToolExecutor
from ..tools.spec import ToolRequest, ToolResult

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
    tools: ToolExecutor | None = None
    snapshots: list[Any] = field(default_factory=list)
    audience_observations: list[Any] = field(default_factory=list)
    existing_creatives: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    daily_budgets: dict[str, float] = field(default_factory=dict)
    campaign_targets: dict[str, dict[str, float]] = field(default_factory=dict)
    # Internal campaign id -> {platform, external_id, name, status}. This is the
    # only place an agent can learn what a campaign is called on its ad network,
    # which is what keeps it from needing a database session of its own.
    campaign_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancellation_check: CancellationCheck | None = None

    def campaign_ref(self, campaign_id: str) -> dict[str, Any] | None:
        """Network coordinates for one campaign, or None when it is not in the run."""
        ref = self.campaign_refs.get(str(campaign_id))
        return dict(ref) if isinstance(ref, dict) else None

    def tool_target(self, campaign_id: str) -> tuple[str, str] | None:
        """Resolve `(platform, external_id)` for a tool call, or None.

        Returning None instead of raising is deliberate: a campaign that has never
        been synced to a network simply cannot be acted on remotely, and the
        caller decides whether that means "skip the preflight" or "report it".
        """
        ref = self.campaign_ref(campaign_id)
        if ref is None:
            return None
        platform = str(ref.get("platform") or "")
        external_id = str(ref.get("external_id") or "")
        if not platform or not external_id:
            return None
        return platform, external_id

    async def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        agent: AgentName,
        idempotency_key: str = "",
        dry_run: bool | None = None,
    ) -> ToolResult | None:
        """Ask the tool layer to run one capability on this agent's behalf.

        Returns None when the tool layer is absent or switched off, so an agent
        written against tools still runs in a degraded deployment instead of
        failing the run. Every other outcome - refusals included - comes back as
        a result the caller can inspect and report.
        """
        if self.tools is None or not self.tools.enabled:
            return None
        return await self.tools.call(
            ToolRequest(
                tool=tool,
                arguments=arguments,
                agent=agent,
                run_id=self.run_id,
                actor=self.actor,
                idempotency_key=idempotency_key,
                dry_run=dry_run,
            )
        )

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
