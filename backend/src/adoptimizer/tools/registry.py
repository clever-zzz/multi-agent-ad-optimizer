"""Tool discovery and per-agent capability filtering."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..core.logging import get_logger
from ..domain.enums import AgentName
from .spec import ToolSpec

logger = get_logger(__name__)


class ToolRegistry:
    """The catalogue of capabilities, and who is allowed to see each one.

    Discovery is per agent on purpose. Handing every agent the full schema list
    is how a creative generator ends up proposing budget changes: the model acts
    on what it is told exists.
    """

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            msg = "Tool already registered: " + spec.name
            raise ValueError(msg)
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def all(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def for_agent(self, agent: AgentName) -> list[ToolSpec]:
        """Everything this agent is permitted to call, sorted by name."""
        return [spec for spec in self.all() if spec.allows(agent)]

    def schemas_for_agent(self, agent: AgentName) -> list[dict[str, Any]]:
        """Function-calling schemas for one agent, ready to hand a model."""
        return [spec.json_schema() for spec in self.for_agent(agent)]

    def describe(self) -> dict[str, Any]:
        """Full catalogue for the admin discovery endpoint."""
        by_agent = {
            agent.value: [spec.name for spec in self.for_agent(agent)] for agent in AgentName
        }
        return {
            "tools": [spec.describe() for spec in self.all()],
            "by_agent": by_agent,
            "write_tools": sorted(name for name, spec in self._specs.items() if not spec.read_only),
        }
