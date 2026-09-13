"""Optimization action approval and execution endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from ...core.deps import (
    ClaimsDep,
    ContainerDep,
    PaginationDep,
    SessionDep,
    client_metadata,
    require,
)
from ...core.errors import PermissionDeniedError
from ...domain.enums import ActionStatus, ActionType, Permission
from ...infra.db.models import OptimizationAction
from ...repositories.runs import ActionRepository
from ...schemas.agent import OptimizationActionOut
from ...schemas.api import (
    ActionDecisionRequest,
    BulkActionRequest,
    BulkActionResult,
    BulkFailedEntry,
)
from ...schemas.common import Page
from ...services.actions import ActionService

router = APIRouter(prefix="/actions", tags=["actions"])


def _service(session: SessionDep, container: ContainerDep) -> ActionService:
    return ActionService(
        session,
        platforms=container.platforms,
        security=container.settings.security,
        tools=container.tools,
    )


def _out(action: OptimizationAction) -> OptimizationActionOut:
    return OptimizationActionOut(
        id=action.id,
        run_id=action.run_id,
        campaign_id=action.campaign_id,
        creative_id=action.creative_id,
        action_type=ActionType(action.action_type),
        status=ActionStatus(action.status),
        before_value=action.before_value,
        after_value=action.after_value,
        reason=action.reason,
        confidence=action.confidence,
        proposed_by=action.proposed_by,
        created_at=action.created_at,
        approved_by=action.approved_by,
        executed_at=action.executed_at,
        external_reference=action.external_reference,
        error_message=action.error_message,
    )


@router.get(
    "",
    response_model=Page[OptimizationActionOut],
    summary="List proposed actions",
    dependencies=[require(Permission.RUN_READ)],
)
async def list_actions(
    session: SessionDep,
    pagination: PaginationDep,
    status_filter: Annotated[ActionStatus | None, Query(alias="status")] = None,
    action_type: Annotated[ActionType | None, Query(alias="type")] = None,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
    run_id: Annotated[str | None, Query(max_length=64)] = None,
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
) -> Page[OptimizationActionOut]:
    """Filterable action queue for the approval screen."""
    repository = ActionRepository(session)
    filters: list[Any] = [OptimizationAction.confidence >= min_confidence]
    if status_filter is not None:
        filters.append(OptimizationAction.status == status_filter.value)
    if action_type is not None:
        filters.append(OptimizationAction.action_type == action_type.value)
    if campaign_id:
        filters.append(OptimizationAction.campaign_id == campaign_id)
    if run_id:
        filters.append(OptimizationAction.run_id == run_id)

    rows, total = await repository.list(
        filters=filters, pagination=pagination, order_by=OptimizationAction.created_at
    )
    return Page[OptimizationActionOut](
        items=[_out(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{action_id}",
    response_model=OptimizationActionOut,
    summary="Fetch one action",
    dependencies=[require(Permission.RUN_READ)],
)
async def get_action(action_id: str, session: SessionDep) -> OptimizationActionOut:
    return _out(await ActionRepository(session).get_or_raise(action_id))


@router.post(
    "/{action_id}/approve",
    response_model=OptimizationActionOut,
    summary="Approve an action",
    dependencies=[require(Permission.ACTION_APPROVE)],
)
async def approve_action(
    action_id: str,
    request: Request,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> OptimizationActionOut:
    """Mark an action as approved so it may be executed."""
    action = await _service(session, container).approve(
        action_id, claims=claims, client=client_metadata(request)
    )
    await session.commit()
    return _out(action)


@router.post(
    "/{action_id}/reject",
    response_model=OptimizationActionOut,
    summary="Reject an action",
    dependencies=[require(Permission.ACTION_APPROVE)],
)
async def reject_action(
    action_id: str,
    payload: ActionDecisionRequest,
    request: Request,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> OptimizationActionOut:
    """Refuse an action and record the operator's reason."""
    action = await _service(session, container).reject(
        action_id, claims=claims, reason=payload.reason, client=client_metadata(request)
    )
    await session.commit()
    return _out(action)


@router.post(
    "/{action_id}/execute",
    response_model=OptimizationActionOut,
    summary="Execute an approved action",
    dependencies=[require(Permission.ACTION_EXECUTE)],
)
async def execute_action(
    action_id: str,
    request: Request,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> OptimizationActionOut:
    """Apply the change locally and push it to the ad platform."""
    action, result = await _service(session, container).execute(
        action_id, claims=claims, client=client_metadata(request)
    )
    await session.commit()
    payload = _out(action)
    if result is not None:
        payload = payload.model_copy(update={"external_reference": result.external_reference})
    return payload


@router.post(
    "/bulk",
    response_model=BulkActionResult,
    summary="Approve or execute many actions",
    dependencies=[require(Permission.ACTION_APPROVE)],
)
async def bulk_actions(
    payload: BulkActionRequest,
    request: Request,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> BulkActionResult:
    """Approve a batch, optionally executing it in the same call.

    Failures are collected rather than aborting the batch, so one bad action
    cannot block the rest of the queue.
    """
    service = _service(session, container)
    metadata = client_metadata(request)
    result = await service.bulk_approve(
        payload.action_ids,
        claims=claims,
        client=metadata,
        min_confidence=payload.min_confidence,
    )

    executed: list[str] = []
    failed: list[BulkFailedEntry] = []
    if payload.execute:
        if not claims.has_permission(Permission.ACTION_EXECUTE):
            raise PermissionDeniedError("Executing actions requires the action:execute permission")
        for action_id in result["approved"]:
            try:
                await service.execute(action_id, claims=claims, client=metadata)
                executed.append(action_id)
            except Exception as exc:
                failed.append(BulkFailedEntry(id=action_id, error=str(exc)[:200]))

    await session.commit()
    return BulkActionResult(
        approved=result["approved"], skipped=result["skipped"], executed=executed, failed=failed
    )
