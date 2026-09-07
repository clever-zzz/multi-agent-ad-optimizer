"""Dashboard and reporting endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from ...core.deps import ClaimsDep, ContainerDep, SessionDep, require
from ...domain.enums import Permission
from ...domain.kpi import PerformanceSnapshot
from ...services.analytics import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/overview",
    summary="Headline KPIs and outstanding work",
    dependencies=[require(Permission.METRICS_READ)],
)
async def overview(
    session: SessionDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    """Everything the landing page renders in one round-trip."""
    return await AnalyticsService(session).overview(days=days)


@router.get(
    "/snapshots",
    summary="Per-campaign performance snapshots",
    dependencies=[require(Permission.METRICS_READ)],
)
async def snapshots(
    session: SessionDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> list[PerformanceSnapshot]:
    """Delivery per campaign for the window, ordered by spend descending."""
    return await AnalyticsService(session).snapshots(days=days)


@router.get(
    "/timeseries",
    summary="Daily delivery trend",
    dependencies=[require(Permission.METRICS_READ)],
)
async def timeseries(
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
) -> list[dict[str, Any]]:
    return await AnalyticsService(session).timeseries(days=days, campaign_id=campaign_id)


@router.get(
    "/campaigns/{campaign_id}",
    summary="Campaign breakdown with scored creatives",
    dependencies=[require(Permission.METRICS_READ)],
)
async def campaign_breakdown(
    campaign_id: str,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    return await AnalyticsService(session).campaign_breakdown(campaign_id, days=days)


@router.get(
    "/llm-spend",
    summary="Model spend by provider",
    dependencies=[require(Permission.SYSTEM_READ)],
)
async def llm_spend(
    session: SessionDep,
    container: ContainerDep,
    claims: ClaimsDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Spend totals plus the configured monthly guardrail."""
    summary = await AnalyticsService(session).spend_summary(days=days)
    summary["monthly_budget_usd"] = container.settings.llm.monthly_budget_usd
    summary["month_to_date_usd"] = round(await container.ledger.month_to_date_usd(), 4)
    _ = claims
    return summary


@router.get(
    "/detect",
    summary="Run anomaly detection without a full optimization",
    dependencies=[require(Permission.METRICS_READ)],
)
async def detect_now(
    session: SessionDep,
    container: ContainerDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
) -> list[dict[str, Any]]:
    """Useful for verifying alert thresholds against current data."""
    from ...services.optimization import OptimizationService

    ids = [campaign_id] if campaign_id else None
    return await OptimizationService(container, session).detect_alerts_now(
        session, campaign_ids=ids, days=days
    )
