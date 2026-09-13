"""Application factory.

The ASGI app is built here rather than at module import so tests can construct
isolated instances with their own settings and container.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api.health import router as health_router
from .api.router import api_router
from .core.config import Environment, Settings, get_settings
from .core.container import Container, build_container
from .core.errors import register_exception_handlers
from .core.logging import configure_logging, get_logger
from .core.middleware import (
    InMemoryRateLimiter,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from .services.optimization import OptimizationService
from .services.seed import seed_database

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container on startup, tear it down on shutdown."""
    settings: Settings = app.state.settings
    container = await build_container(settings)
    app.state.container = container

    if settings.database.is_sqlite:
        await _bootstrap_sqlite(container, settings)

    async with container.database.unit_of_work() as session:
        reaped = await OptimizationService(container, session).reap_stale_runs(session)
        await session.commit()
    if reaped:
        logger.warning("startup_reaped_stale_runs", count=reaped)

    logger.info("application_started", environment=settings.app.environment.value)
    try:
        yield
    finally:
        await container.shutdown()
        logger.info("application_stopped")


async def _bootstrap_sqlite(container: Container, settings: Settings) -> None:
    """Create the schema and the bootstrap administrator on first run."""
    async with container.database.unit_of_work() as session:
        await seed_database(
            session,
            admin_email=settings.security.bootstrap_admin_email,
            admin_password=settings.security.bootstrap_admin_password.get_secret_value(),
            security=settings.security,
        )
        await session.commit()
    _ = container


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the FastAPI application."""
    cfg = settings or get_settings()
    configure_logging(level=cfg.observability.log_level, json_logs=cfg.observability.json_logs)

    is_production = cfg.app.environment == Environment.PRODUCTION
    app = FastAPI(
        title=cfg.app.name,
        version=cfg.app.version,
        summary="Multi-agent advertising optimization platform",
        description=(
            "Runs a six-agent LangGraph supervisor loop over campaign telemetry and "
            "proposes budget, bid, creative and experiment changes. Nothing is executed "
            "without an explicit approval."
        ),
        docs_url="/docs" if not is_production else None,
        redoc_url="/redoc" if not is_production else None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = cfg

    # Starlette runs the last-added middleware outermost, so this list is written
    # inside-out. Request context has to wrap the rate limiter: a throttled reply
    # is produced before the router is reached, and without this order it carries
    # neither a correlation id nor an X-Request-ID header. Security headers wrap it
    # too so 429s are hardened exactly like every other response.
    app.add_middleware(
        RateLimitMiddleware,
        cfg,
        InMemoryRateLimiter(),
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.app.cors_allow_origins,
        allow_credentials=cfg.app.cors_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    if cfg.app.trusted_hosts and cfg.app.trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.app.trusted_hosts)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=cfg.app.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": cfg.app.name,
            "version": cfg.app.version,
            "environment": cfg.app.environment.value,
            "docs": "/docs",
            "api": cfg.app.api_v1_prefix,
        }

    logger.info(
        "application_created",
        environment=cfg.app.environment.value,
        routes=len(app.routes),
    )
    return app
