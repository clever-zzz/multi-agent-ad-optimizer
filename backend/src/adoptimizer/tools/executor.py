"""The choke point between an agent's intent and the outside world.

Everything the system can *do* - not just propose - passes through
:meth:`ToolExecutor.call`. It answers five questions in a fixed order, and only
the last one touches a handler:

1. does this tool exist;
2. may this caller use it;
3. has this run spent its call budget;
4. have we already run this exact request (idempotency);
5. do the arguments validate.

Then it decides between executing and dry-running. A write requested by an agent
is dry-run unless ``TOOLS__ALLOW_AGENT_WRITES`` is on, which is what keeps a
model from moving real money no matter what it reasons. Every outcome - the
refusals included - is published on the event bus and persisted, so "the agent
tried to pause campaign X and was stopped" is a queryable fact.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Protocol

from pydantic import ValidationError

from ..core.config import ToolSettings
from ..core.logging import get_logger
from ..orchestrator.events import EventBus
from .registry import ToolRegistry
from .spec import (
    BENIGN_OUTCOMES,
    REFUSED_OUTCOMES,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    ToolSpec,
)

logger = get_logger(__name__)


class ToolAuditSink(Protocol):
    """Where invocation records go. Must never raise into the caller."""

    async def record(self, result: ToolResult) -> None: ...


class NullToolAuditSink:
    """Default sink: the event bus already carries the record."""

    async def record(self, result: ToolResult) -> None:
        _ = result


class ToolExecutor:
    """Validates, authorises, budgets, dry-runs and audits tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        settings: ToolSettings,
        bus: EventBus | None = None,
        sink: ToolAuditSink | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._bus = bus
        self._sink: ToolAuditSink = sink or NullToolAuditSink()
        self._calls_per_run: dict[str, int] = defaultdict(int)
        self._replays: dict[str, ToolResult] = {}
        # Every invocation is counted here, including the ones refused before the
        # budget gate. _calls_per_run only tracks budget consumption, which is not
        # the same number: a permission denial never reaches it.
        self._stats_per_run: dict[str, dict[str, Any]] = defaultdict(_new_run_stats)

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def dry_run_by_default(self) -> bool:
        return self._settings.dry_run

    async def call(self, request: ToolRequest) -> ToolResult:
        """Run one tool call, or refuse it with a reason the caller can act on."""
        started = time.perf_counter()
        spec = self._registry.get(request.tool)

        if not self._settings.enabled:
            return await self._finish(
                request, spec, ToolOutcome.DISABLED, started, error="tool layer is disabled"
            )
        if spec is None:
            return await self._finish(
                request,
                None,
                ToolOutcome.VALIDATION_FAILED,
                started,
                error="unknown tool: " + request.tool,
            )
        if not spec.allows(request.agent):
            return await self._finish(
                request,
                spec,
                ToolOutcome.PERMISSION_DENIED,
                started,
                error=(
                    "agent "
                    + (request.agent.value if request.agent else "unknown")
                    + " may not call "
                    + spec.name
                ),
            )

        # Only agent calls are budgeted. The cap exists to bound what a model
        # can do on its own initiative; a human-approved execution has already
        # cleared a stronger gate, and letting one busy run's preflights starve
        # the operator's approval would make the guardrail the cause of the
        # outage it is there to prevent. Those calls are still audited per run.
        if request.run_id and request.from_agent:
            used = self._calls_per_run[request.run_id]
            if used >= self._settings.max_calls_per_run:
                return await self._finish(
                    request,
                    spec,
                    ToolOutcome.BUDGET_EXHAUSTED,
                    started,
                    error=(
                        "run "
                        + request.run_id
                        + " reached its tool budget of "
                        + str(self._settings.max_calls_per_run)
                    ),
                )
            self._calls_per_run[request.run_id] = used + 1

        replay_key = self._replay_key(request)
        if replay_key and replay_key in self._replays:
            previous = self._replays[replay_key]
            replayed = ToolResult(
                request=request,
                outcome=ToolOutcome.REPLAYED,
                spec=spec,
                data=dict(previous.data),
                error=previous.error,
                duration_ms=_elapsed_ms(started),
            )
            return await self._finish(request, spec, ToolOutcome.REPLAYED, started, result=replayed)

        try:
            arguments = spec.parameters.model_validate(request.arguments)
        except ValidationError as exc:
            return await self._finish(
                request,
                spec,
                ToolOutcome.VALIDATION_FAILED,
                started,
                error=_format_validation_error(exc),
            )

        if self._should_dry_run(request, spec):
            return await self._finish(
                request,
                spec,
                ToolOutcome.DRY_RUN,
                started,
                result=ToolResult(
                    request=request,
                    outcome=ToolOutcome.DRY_RUN,
                    spec=spec,
                    data={
                        "would_call": spec.name,
                        "arguments": dict(request.arguments),
                        "reason": _dry_run_reason(request, self._settings),
                    },
                    duration_ms=_elapsed_ms(started),
                ),
            )

        try:
            payload = await spec.handler(arguments, request)
        except Exception as exc:
            logger.warning(
                "tool_call_failed",
                tool=spec.name,
                agent=request.agent.value if request.agent else None,
                run_id=request.run_id or None,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return await self._finish(
                request,
                spec,
                ToolOutcome.FAILED,
                started,
                error=type(exc).__name__ + ": " + str(exc),
            )

        result = ToolResult(
            request=request,
            outcome=ToolOutcome.SUCCESS,
            spec=spec,
            data=payload,
            duration_ms=_elapsed_ms(started),
        )
        if replay_key:
            self._replays[replay_key] = result
        return await self._finish(request, spec, ToolOutcome.SUCCESS, started, result=result)

    def usage(self, run_id: str) -> dict[str, Any]:
        """Per-run counters, folded into the run summary.

        Two call counts are reported because they answer different questions.
        ``calls`` is budget consumption - what the guardrail charged the run,
        which is agent traffic only, since a human-approved execution is not
        budgeted.
        ``invocations`` is everything the executor saw, refusals included,
        which is what an operator needs to answer "did the agents try anything
        odd on this run".
        """
        stats = self._stats_per_run.get(run_id) or _new_run_stats()
        return {
            "calls": self._calls_per_run.get(run_id, 0),
            "invocations": int(stats["invocations"]),
            "budget": self._settings.max_calls_per_run,
            "by_outcome": dict(stats["by_outcome"]),
            "by_tool": dict(stats["by_tool"]),
            "writes": int(stats["writes"]),
            "dry_runs": int(stats["dry_runs"]),
            "refusals": int(stats["refusals"]),
            "failures": int(stats["failures"]),
            "dry_run_by_default": self._settings.dry_run,
            "agent_writes_allowed": self._settings.allow_agent_writes,
            "tools_registered": len(self._registry),
        }

    def _count(self, request: ToolRequest, spec: ToolSpec | None, result: ToolResult) -> None:
        """Fold one finished invocation into the per-run counters."""
        stats = self._stats_per_run[request.run_id]
        stats["invocations"] = int(stats["invocations"]) + 1
        by_outcome: dict[str, int] = stats["by_outcome"]
        by_outcome[result.outcome.value] = by_outcome.get(result.outcome.value, 0) + 1
        by_tool: dict[str, int] = stats["by_tool"]
        by_tool[request.tool] = by_tool.get(request.tool, 0) + 1
        if spec is not None and not spec.read_only:
            stats["writes"] = int(stats["writes"]) + 1
        if result.dry_run:
            stats["dry_runs"] = int(stats["dry_runs"]) + 1
        if result.outcome in REFUSED_OUTCOMES:
            stats["refusals"] = int(stats["refusals"]) + 1
        if result.outcome is ToolOutcome.FAILED:
            stats["failures"] = int(stats["failures"]) + 1

    def _should_dry_run(self, request: ToolRequest, spec: ToolSpec) -> bool:
        """Agents preflight; only an explicit caller can ask for a real write."""
        if spec.read_only:
            return False
        if request.from_agent and not self._settings.allow_agent_writes:
            return True
        if request.dry_run is not None:
            return request.dry_run
        return self._settings.dry_run

    def _replay_key(self, request: ToolRequest) -> str:
        if not request.idempotency_key:
            return ""
        return request.run_id + "|" + request.idempotency_key

    async def _finish(
        self,
        request: ToolRequest,
        spec: ToolSpec | None,
        outcome: ToolOutcome,
        started: float,
        *,
        error: str | None = None,
        result: ToolResult | None = None,
    ) -> ToolResult:
        """Publish, persist and normalise one outcome."""
        final = result or ToolResult(
            request=request,
            outcome=outcome,
            spec=spec,
            error=error,
            duration_ms=time.perf_counter() - started,
        )
        if final.duration_ms <= 0:
            final.duration_ms = _elapsed_ms(started)

        if request.run_id:
            self._count(request, spec, final)

        logger.info(
            "tool_invoked",
            tool=request.tool,
            outcome=final.outcome.value,
            agent=request.agent.value if request.agent else None,
            run_id=request.run_id or None,
            read_only=bool(spec.read_only) if spec else None,
            duration_ms=round(final.duration_ms, 2),
            error=final.error,
        )
        if self._bus is not None and request.run_id:
            await self._bus.publish(
                request.run_id,
                "tool.invoked",
                agent=request.agent.value if request.agent else "system",
                payload={
                    "tool": request.tool,
                    "outcome": final.outcome.value,
                    "dry_run": final.dry_run,
                    "read_only": bool(spec.read_only) if spec else False,
                    "duration_ms": round(final.duration_ms, 2),
                    "error": final.error,
                },
            )
        try:
            await self._sink.record(final)
        except Exception as exc:
            # Auditing must never be the reason a run fails.
            logger.warning("tool_audit_failed", tool=request.tool, error=str(exc))
        return final


def _new_run_stats() -> dict[str, Any]:
    """Zeroed counters for one run, created the first time that run is seen."""
    return {
        "invocations": 0,
        "by_outcome": {},
        "by_tool": {},
        "writes": 0,
        "dry_runs": 0,
        "refusals": 0,
        "failures": 0,
    }


def _elapsed_ms(started: float) -> float:
    """Milliseconds since ``started``, the unit every duration field reports."""
    return (time.perf_counter() - started) * 1000


def _dry_run_reason(request: ToolRequest, settings: ToolSettings) -> str:
    """Explain the interlock that fired, in words an operator can act on."""
    if request.from_agent and not settings.allow_agent_writes:
        return (
            "agent-initiated writes are preflighted only; set TOOLS__ALLOW_AGENT_WRITES=true "
            "to let "
            + (request.agent.value if request.agent else "an agent")
            + " mutate a live platform"
        )
    if request.dry_run:
        return "caller requested a dry run"
    return "TOOLS__DRY_RUN=true: paper-trading mode, nothing reaches the platform"


def _format_validation_error(exc: ValidationError) -> str:
    """Compress a pydantic error into one readable line."""
    parts = [
        str(err.get("loc") or ("?",)).replace("'", "") + " " + str(err.get("msg") or "")
        for err in exc.errors()
    ]
    return "invalid arguments: " + "; ".join(parts[:5])


__all__ = [
    "BENIGN_OUTCOMES",
    "NullToolAuditSink",
    "ToolAuditSink",
    "ToolExecutor",
]
