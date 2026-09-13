"""The tool layer: capability catalogue, executor and audit trail.

Agents do not import platform clients, database sessions or HTTP libraries.
They ask for a named tool and receive a result. That indirection is what makes
capability, permission, budget and dry-run enforceable in one place instead of
being a convention every future agent has to remember to follow.
"""

from __future__ import annotations

from ..core.config import ToolSettings
from ..infra.ads.registry import PlatformRegistry
from ..orchestrator.events import EventBus
from .audit import DatabaseToolAudit
from .executor import NullToolAuditSink, ToolAuditSink, ToolExecutor
from .platform import PlatformTools, build_platform_tools
from .registry import ToolRegistry
from .spec import (
    BENIGN_OUTCOMES,
    Handler,
    ToolArguments,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "BENIGN_OUTCOMES",
    "DatabaseToolAudit",
    "Handler",
    "NullToolAuditSink",
    "PlatformTools",
    "ToolArguments",
    "ToolAuditSink",
    "ToolExecutor",
    "ToolOutcome",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ToolSettings",
    "ToolSpec",
    "build_platform_tools",
    "build_tool_executor",
]


def build_tool_executor(
    platforms: PlatformRegistry,
    settings: ToolSettings,
    *,
    bus: EventBus | None = None,
    sink: ToolAuditSink | None = None,
    extra: list[ToolSpec] | None = None,
) -> ToolExecutor:
    """Assemble the registry and executor used by the composition root."""
    specs = build_platform_tools(platforms)
    if extra:
        specs.extend(extra)
    return ToolExecutor(ToolRegistry(specs), settings=settings, bus=bus, sink=sink)
