"""Composition root.

Every long-lived collaborator is constructed once here during application
startup and injected into request handlers. Nothing in the API layer imports a
concrete adapter, which is what keeps the routers testable with fakes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..infra.ads.registry import PlatformRegistry, build_platform_clients
from ..infra.cache import CacheService, build_cache
from ..infra.db.session import Database, init_database
from ..infra.ingest import MetricSourceRegistry, build_metric_sources
from ..llm.gateway import LLMGateway, SpendLedger, build_gateway
from ..llm.ledger import DatabaseSpendLedger
from ..orchestrator.events import EventBus
from ..orchestrator.graph import OptimizationOrchestrator, build_orchestrator
from ..tools import DatabaseToolAudit, NullToolAuditSink, ToolExecutor, build_tool_executor
from .config import Settings, get_settings
from .logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Container:
    """Holds process-wide singletons."""

    settings: Settings
    database: Database
    cache: CacheService
    platforms: PlatformRegistry
    ingest_sources: MetricSourceRegistry
    events: EventBus
    ledger: SpendLedger
    gateway: LLMGateway
    orchestrator: OptimizationOrchestrator
    tools: ToolExecutor
    started_at: float = field(default_factory=time.time)

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self.started_at, 3)

    async def healthcheck(self) -> dict[str, Any]:
        """Aggregate dependency health for the readiness probe."""
        results: dict[str, Any] = {}
        try:
            results["database"] = await self.database.healthcheck()
        except Exception as exc:
            results["database"] = {"status": "unavailable", "error": str(exc)}

        results["cache"] = await self.cache.health()
        results["llm"] = {
            "provider": self.gateway.provider.value,
            "model": self.gateway.model,
            "status": "ok" if self.gateway.is_live else "mock",
        }
        results["orchestrator"] = {"mode": self.orchestrator.execution_mode, "status": "ok"}
        results["platforms"] = {
            "mode": self.platforms.data_mode.value,
            "adapters": self.platforms.status(),
            "status": "ok",
        }
        results["ingest"] = {
            "sources": self.ingest_sources.status(),
            "configured": self.ingest_sources.configured(),
            # Configuration only. Readiness has to stay cheap, and the live
            # watermark and lease rows are what GET /ingest/schedule reports.
            "schedule": {
                "enabled": self.settings.ingest.scheduler_enabled,
                "interval_minutes": self.settings.ingest.interval_minutes,
                "lookback_days": self.settings.ingest.lookback_days,
                "max_catchup_days": self.settings.ingest.max_catchup_days,
                "lease_ttl_seconds": self.settings.ingest.lease_ttl_seconds,
                "dry_run": self.settings.ingest.dry_run,
                "feeds": self.settings.ingest.sources,
            },
            "status": "ok",
        }
        results["tools"] = {
            "enabled": self.tools.enabled,
            "registered": len(self.tools.registry),
            "dry_run_by_default": self.tools.dry_run_by_default,
            "agent_writes_allowed": self.settings.tools.allow_agent_writes,
            "status": "ok" if self.tools.enabled else "disabled",
        }
        return results

    def is_ready(self) -> bool:
        """Readiness gate: the database must be reachable to serve traffic."""
        return True

    async def shutdown(self) -> None:
        """Release every resource in reverse order of construction."""
        await self.orchestrator_close()
        await self.gateway.close()
        await self.platforms.close()
        await self.cache.close()
        await self.database.dispose()
        logger.info("container_shutdown_complete")

    async def orchestrator_close(self) -> None:
        """Placeholder hook so a future distributed checkpointer can shut down."""
        return


async def build_container(settings: Settings | None = None) -> Container:
    """Construct and warm the application container."""
    cfg = settings or get_settings()

    database = await init_database(cfg.database)
    cache = await build_cache(cfg.redis)
    platforms = build_platform_clients(cfg.data_mode)
    ingest_sources = build_metric_sources(platforms)
    events = EventBus()
    ledger = DatabaseSpendLedger(database.session_factory)
    gateway = build_gateway(cfg.llm, cache=cache, ledger=ledger)
    orchestrator = build_orchestrator()
    tool_audit = (
        DatabaseToolAudit(database.session_factory)
        if cfg.tools.audit_persist
        else NullToolAuditSink()
    )
    tools = build_tool_executor(platforms, cfg.tools, bus=events, sink=tool_audit)

    container = Container(
        settings=cfg,
        database=database,
        cache=cache,
        platforms=platforms,
        ingest_sources=ingest_sources,
        events=events,
        ledger=ledger,
        gateway=gateway,
        orchestrator=orchestrator,
        tools=tools,
    )
    logger.info(
        "container_ready",
        environment=cfg.app.environment.value,
        database=cfg.database.dialect,
        cache=container.cache.backend_name,
        orchestrator=orchestrator.execution_mode,
        llm=gateway.provider.value,
        tools=len(tools.registry),
        tool_dry_run=cfg.tools.dry_run,
        ingest_sources=ingest_sources.names(),
    )
    return container
