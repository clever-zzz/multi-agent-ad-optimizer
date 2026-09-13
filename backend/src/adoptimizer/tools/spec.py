"""Tool contracts: what an agent may do, and what happened when it tried.

An agent never imports a platform client. It asks the executor to run a named
tool with a JSON-shaped argument object. The executor validates, authorises,
budgets, audits - and for anything that mutates a real ad account, dry-runs it
first. That single choke point is what turns "the model decided to spend money"
from a stack trace into an auditable sentence.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..domain.enums import AgentName


class ToolArguments(BaseModel):
    """Base class for every tool parameter model.

    ``extra="forbid"`` is the point: a caller that hallucinates a field must get
    a validation error, not a silently ignored argument. ``frozen=True`` keeps a
    handler from mutating the request it was given.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutcome(StrEnum):
    """Terminal state of one invocation."""

    SUCCESS = "success"
    DRY_RUN = "dry_run"
    REPLAYED = "replayed"
    VALIDATION_FAILED = "validation_failed"
    PERMISSION_DENIED = "permission_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DISABLED = "disabled"
    FAILED = "failed"


# Outcomes that mean "nothing went wrong", used for summary counters.
BENIGN_OUTCOMES = frozenset({ToolOutcome.SUCCESS, ToolOutcome.DRY_RUN, ToolOutcome.REPLAYED})

# Outcomes where a guardrail said no. Counted separately from FAILED because
# "the executor refused" and "the platform errored" call for different fixes:
# one is a policy or schema question, the other is an integration question.
REFUSED_OUTCOMES = frozenset(
    {
        ToolOutcome.VALIDATION_FAILED,
        ToolOutcome.PERMISSION_DENIED,
        ToolOutcome.BUDGET_EXHAUSTED,
        ToolOutcome.DISABLED,
    }
)

# Refusals that mean the proposal itself cannot be carried out, so a human must
# not be asked to approve it. Budget exhaustion and a disabled tool layer are
# deliberately absent: those are deployment states, not defects in the proposal,
# and suppressing a sound proposal because of them would hide real work.
BLOCKING_REFUSALS = frozenset({ToolOutcome.VALIDATION_FAILED, ToolOutcome.PERMISSION_DENIED})


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One caller's ask, before any validation.

    ``agent`` is ``None`` for a human-driven caller such as the action executor.
    That distinction is load-bearing: an agent-initiated write is dry-run unless
    ``TOOLS__ALLOW_AGENT_WRITES`` is on, while an approved execution is not.
    """

    tool: str
    arguments: dict[str, Any]
    agent: AgentName | None = None
    run_id: str = ""
    actor: str = "system"
    idempotency_key: str = ""
    dry_run: bool | None = None

    @property
    def from_agent(self) -> bool:
        return self.agent is not None


Handler = Callable[[ToolArguments, ToolRequest], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A capability, its schema, who may use it, and what runs it."""

    name: str
    description: str
    parameters: type[ToolArguments]
    handler: Handler
    read_only: bool = True
    agents: frozenset[AgentName] = frozenset()
    touches_platform: bool = False

    def allows(self, agent: AgentName | None) -> bool:
        """An empty allow-list means every agent; a filled one is a whitelist."""
        if agent is None:
            return True
        return not self.agents or agent in self.agents

    def json_schema(self) -> dict[str, Any]:
        """Function-calling shape, generated from the model rather than by hand."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters.model_json_schema(),
            },
        }

    def describe(self) -> dict[str, Any]:
        """Discovery payload for the admin API and the run timeline."""
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "touches_platform": self.touches_platform,
            "agents": sorted(agent.value for agent in self.agents),
            "parameters": self.parameters.model_json_schema(),
        }


@dataclass(slots=True)
class ToolResult:
    """What the executor decided, and what the handler returned."""

    request: ToolRequest
    outcome: ToolOutcome
    spec: ToolSpec | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome in BENIGN_OUTCOMES

    @property
    def dry_run(self) -> bool:
        return self.outcome is ToolOutcome.DRY_RUN

    @property
    def refused(self) -> bool:
        """True when a guardrail declined the call rather than running it."""
        return self.outcome in REFUSED_OUTCOMES

    @property
    def blocking(self) -> bool:
        """True when the refusal invalidates the proposal behind the call."""
        return self.outcome in BLOCKING_REFUSALS or self.outcome is ToolOutcome.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.request.tool,
            "agent": self.request.agent.value if self.request.agent else None,
            "outcome": self.outcome.value,
            "dry_run": self.dry_run,
            "duration_ms": round(self.duration_ms, 2),
            "data": self.data,
            "error": self.error,
        }
