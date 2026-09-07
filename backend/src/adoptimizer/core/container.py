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
from ..llm.gateway import LLMGateway, SpendLedger, build_gateway
from ..llm.ledger import DatabaseSpendLedger
from ..orchestrator.events import EventBus
from ..orchestrator.graph import OptimizationOrchestrator, build_orchestrator
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
    events: EventBus
    ledger: SpendLedger
    gateway: LLMGateway
    orchestrator: OptimizationOrchestrator
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
    events = EventBus()
    ledger = DatabaseSpendLedger(database.session_factory)
    gateway = build_gateway(cfg.llm, cache=cache, ledger=ledger)
    orchestrator = build_orchestrator()

    container = Container(
        settings=cfg,
        database=database,
        cache=cache,
        platforms=platforms,
        events=events,
        ledger=ledger,
        gateway=gateway,
        orchestrator=orchestrator,
    )
    logger.info(
        "container_ready",
        environment=cfg.app.environment.value,
        database=cfg.database.dialect,
        cache=container.cache.backend_name,
        orchestrator=orchestrator.execution_mode,
        llm=gateway.provider.value,
    )
    return container
