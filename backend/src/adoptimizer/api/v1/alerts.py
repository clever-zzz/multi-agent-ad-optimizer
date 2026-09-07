"""Alert endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from ...core.deps import ClaimsDep, PaginationDep, SessionDep, client_metadata, require
from ...domain.enums import AlertRule, AlertSeverity, AlertStatus, Permission
from ...infra.db.models import Alert
from ...repositories.alerts import AlertRepository
from ...schemas.agent import AlertOut, AlertSummaryOut, AlertTrendPoint
from ...schemas.common import Page
from ...services.audit import AuditService

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=alert.id,
        campaign_id=alert.campaign_id,
        run_id=alert.run_id,
        rule=AlertRule(alert.rule),
        severity=AlertSeverity(alert.severity),
        status=AlertStatus(alert.status),
        observed=alert.observed,
        threshold=alert.threshold,
        message=alert.message,
        dedup_key=alert.dedup_key,
        context=alert.context or {},
        detected_at=alert.detected_at,
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
    )


@router.get(
    "",
    response_model=Page[AlertOut],
    summary="List alerts",
    dependencies=[require(Permission.ALERT_READ)],
)
async def list_alerts(
    session: SessionDep,
    pagination: PaginationDep,
    status_filter: Annotated[AlertStatus | None, Query(alias="status")] = None,
    severity: Annotated[AlertSeverity | None, Query()] = None,
    rule: Annotated[AlertRule | None, Query()] = None,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[AlertOut]:
    """Paginated alert queue with the filters the on-call view needs."""
    repository = AlertRepository(session)
    filters: list[Any] = []
    if status_filter is not None:
        filters.append(Alert.status == status_filter.value)
    if severity is not None:
        filters.append(Alert.severity == severity.value)
    if rule is not None:
        filters.append(Alert.rule == rule.value)
    if campaign_id:
        filters.append(Alert.campaign_id == campaign_id)

    rows, total = await repository.list(
        filters=filters, pagination=pagination, order_by=Alert.detected_at
    )
    return Page[AlertOut](
        items=[_out(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/summary",
    response_model=AlertSummaryOut,
    summary="Alert counts by severity and status",
    dependencies=[require(Permission.ALERT_READ)],
)
async def alert_summary(
    session: SessionDep,
    container_days: Annotated[int, Query(ge=1, le=365, alias="days")] = 30,
) -> AlertSummaryOut:
    """Counts for the sidebar badge, the KPI tiles and the trend table."""
    repository = AlertRepository(session)
    by_status = await repository.counts_by_status()
    resolved = by_status.get(AlertStatus.RESOLVED.value, 0)
    history = await repository.history(days=container_days)
    return AlertSummaryOut(
        # Derived rather than read straight off the status bucket so the badge
        # keeps matching "not yet resolved" if another status is added later.
        open_alerts=sum(by_status.values()) - resolved,
        acknowledged=by_status.get(AlertStatus.ACKNOWLEDGED.value, 0),
        resolved=resolved,
        by_severity=await repository.counts_by_severity(),
        history=[AlertTrendPoint.model_validate(row) for row in history],
    )


@router.post(
    "/{alert_id}/acknowledge",
    response_model=AlertOut,
    summary="Acknowledge an alert",
    dependencies=[require(Permission.ALERT_ACK)],
)
async def acknowledge_alert(
    alert_id: str, request: Request, claims: ClaimsDep, session: SessionDep
) -> AlertOut:
    alert = await AlertRepository(session).acknowledge(alert_id, actor_id=claims.subject)
    await AuditService(session).record(
        action="alert.acknowledged",
        resource_type="alert",
        resource_id=alert_id,
        claims=claims,
        client=client_metadata(request),
    )
    await session.commit()
    return _out(alert)


@router.post(
    "/{alert_id}/resolve",
    response_model=AlertOut,
    summary="Resolve an alert",
    dependencies=[require(Permission.ALERT_ACK)],
)
async def resolve_alert(
    alert_id: str, request: Request, claims: ClaimsDep, session: SessionDep
) -> AlertOut:
    alert = await AlertRepository(session).resolve(alert_id, actor_id=claims.subject)
    await AuditService(session).record(
        action="alert.resolved",
        resource_type="alert",
        resource_id=alert_id,
        claims=claims,
        client=client_metadata(request),
    )
    await session.commit()
    return _out(alert)
