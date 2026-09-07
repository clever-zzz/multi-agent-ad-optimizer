"""Liveness, readiness and Prometheus endpoints.

Kept outside the versioned API and unauthenticated so orchestrators and load
balancers can probe without credentials. Readiness reports degraded dependencies
explicitly instead of pretending everything is fine.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from ..core.deps import ContainerDep
from ..core.metrics import render_metrics
from ..schemas.common import HealthResponse

router = APIRouter(tags=["system"])

CRITICAL_DEPENDENCIES = frozenset({"database"})


@router.get("/healthz", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    """Succeeds whenever the process can serve requests."""
    return {"status": "alive"}


@router.get("/readyz", response_model=HealthResponse, summary="Readiness probe")
async def readiness(container: ContainerDep) -> Response:
    """Reports dependency health and fails when a critical one is down."""
    settings = container.settings
    dependencies = await container.healthcheck()

    degraded = [
        name
        for name, state in dependencies.items()
        if isinstance(state, dict) and state.get("status") in ("unavailable", "error")
    ]
    critical_down = [name for name in degraded if name in CRITICAL_DEPENDENCIES]
    healthy = not critical_down

    payload = HealthResponse(
        status="ready" if healthy else "not_ready",
        version=settings.app.version,
        environment=settings.app.environment.value,
        uptime_seconds=container.uptime_seconds,
        dependencies=dependencies,
    )
    return Response(
        content=payload.model_dump_json(),
        status_code=200 if healthy else 503,
        media_type="application/json",
    )


@router.get("/metrics", summary="Prometheus metrics")
async def metrics(container: ContainerDep) -> Response:
    """Exposes the Prometheus text format."""
    if not container.settings.observability.metrics_enabled:
        return Response(status_code=404)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


@router.get("/system/info", response_model=dict[str, Any], summary="Runtime configuration")
async def system_info(container: ContainerDep) -> dict[str, Any]:
    """Non-sensitive runtime facts for the status page."""
    info = container.settings.public_dict()
    info["orchestrator_mode"] = container.orchestrator.execution_mode
    info["cache_backend"] = container.cache.backend_name
    info["uptime_seconds"] = container.uptime_seconds
    return info
