"""Optimization run endpoints, including the live SSE stream."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from ...core.deps import (
    ClaimsDep,
    ContainerDep,
    PaginationDep,
    SessionDep,
    client_metadata,
    require,
)
from ...core.logging import get_logger
from ...domain.enums import Permission, RunStatus
from ...orchestrator.events import TERMINAL_EVENTS, AgentEvent
from ...repositories.runs import RunRepository
from ...schemas.api import RunDetailOut, RunEventOut, RunOut, RunStartRequest
from ...schemas.common import Page
from ...services.audit import AuditService
from ...services.optimization import OptimizationService

logger = get_logger(__name__)

router = APIRouter(prefix="/runs", tags=["optimization-runs"])

IDEMPOTENCY_HEADER = "Idempotency-Key"

# The durable log is the source of truth; the in-process bus is only a
# low-latency tail. These two values decide how often a quiet stream falls back
# to re-reading the log, which is what makes it survive a restart and work on a
# replica that is not the one executing the run.
STREAM_IDLE_TIMEOUT_SECONDS = 15.0
STREAM_HEARTBEATS_PER_RESYNC = 4


@router.post(
    "",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an optimization run",
    dependencies=[require(Permission.RUN_TRIGGER)],
)
async def start_run(
    payload: RunStartRequest,
    request: Request,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> Any:
    """Queue a run. With background=false the call blocks until it completes."""
    idempotency_key = request.headers.get(IDEMPOTENCY_HEADER)
    service = OptimizationService(container, session)
    metadata = client_metadata(request)

    run = await service.start_run(
        session,
        campaign_ids=payload.campaign_ids,
        max_iterations=payload.max_iterations,
        window_days=payload.window_days,
        trigger_type="api",
        actor_id=claims.subject,
        idempotency_key=idempotency_key,
        background=payload.background,
    )
    await AuditService(session).record(
        action="run.started",
        resource_type="optimization_run",
        resource_id=run.id,
        claims=claims,
        after=payload.model_dump(),
        client=metadata,
    )
    await session.commit()

    # An idempotency-key replay hands back the already-finished run without
    # dispatching a task, so there is nothing to wait on; blocking would surface a
    # 404 for a run that plainly exists.
    if not payload.background and not RunStatus(run.status).is_terminal:
        await service.wait_for(run.id, timeout=container.settings.app.request_timeout_seconds * 4)
        # The run executes in a task that owns a separate session and commits there.
        # This request's session is built with expire_on_commit=False, so its identity
        # map still holds the row exactly as it was at dispatch time, and its open
        # transaction pins a snapshot that predates the worker's commit. End the
        # transaction and reload explicitly, otherwise a finished run is reported as
        # "pending" forever.
        await session.commit()
        await session.refresh(run)

    return RunOut.model_validate(run)


@router.get(
    "",
    response_model=Page[RunOut],
    summary="List runs",
    dependencies=[require(Permission.RUN_READ)],
)
async def list_runs(
    session: SessionDep,
    pagination: PaginationDep,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> Page[RunOut]:
    """Paginated run history."""
    from ...infra.db.models import OptimizationRun
    from ...repositories.runs import RunRepository

    filters = [OptimizationRun.status == status_filter.value] if status_filter else []
    rows, total = await RunRepository(session).list(
        filters=filters,
        pagination=pagination,
        order_by=OptimizationRun.created_at,
    )
    return Page[RunOut](
        items=[RunOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{run_id}",
    response_model=RunDetailOut,
    summary="Fetch a run with its timeline and outputs",
    dependencies=[require(Permission.RUN_READ)],
)
async def get_run(run_id: str, session: SessionDep, container: ContainerDep) -> RunDetailOut:
    detail = await OptimizationService(container, session).run_detail(session, run_id)
    return RunDetailOut(
        run=RunOut.model_validate(detail["run"]),
        events=[RunEventOut.model_validate(event) for event in detail["events"]],
        actions=[_action_out(action) for action in detail["actions"]],
        allocations=[_allocation_out(item) for item in detail["allocations"]],
    )


@router.post(
    "/{run_id}/cancel",
    response_model=RunOut,
    summary="Cancel a run",
    dependencies=[require(Permission.RUN_TRIGGER)],
)
async def cancel_run(
    run_id: str, claims: ClaimsDep, session: SessionDep, container: ContainerDep
) -> RunOut:
    """Cancel a queued or executing run."""
    service = OptimizationService(container, session)
    run = await service.cancel(session, run_id, actor_id=claims.subject)
    await AuditService(session).record(
        action="run.cancelled", resource_type="optimization_run", resource_id=run_id, claims=claims
    )
    await session.commit()
    return RunOut.model_validate(run)


@router.get(
    "/{run_id}/stream",
    summary="Stream run progress over SSE",
    dependencies=[require(Permission.RUN_READ)],
)
async def stream_run(
    run_id: str,
    container: ContainerDep,
    last_event_id: Annotated[str | None, Query(alias="lastSeq")] = None,
) -> StreamingResponse:
    """Server-sent events for live progress, resumable via lastSeq.

    Each pass emits everything the durable ``run_events`` log holds past the
    client cursor, then tails the in-process bus. When the tail goes quiet the
    loop re-reads the log and re-checks the run status, so the stream terminates
    correctly even when the run executed in another process or before a restart.
    """
    async with container.database.unit_of_work() as session:
        await RunRepository(session).get_or_raise(run_id)

    after_seq = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    def _render(event: AgentEvent) -> str:
        payload = json.dumps(event.to_sse(), ensure_ascii=False, default=str)
        return (
            "id: " + str(event.seq) + "\nevent: " + event.event_type + "\ndata: " + payload + "\n\n"
        )

    async def event_source() -> Any:
        cursor = after_seq
        try:
            while True:
                async with container.database.unit_of_work() as session:
                    repository = RunRepository(session)
                    stored = await repository.events(run_id, after_seq=cursor, limit=500)
                    status = await repository.status_of(run_id)

                for record in stored:
                    cursor = max(cursor, record.seq)
                    yield _render(
                        AgentEvent(
                            run_id=record.run_id,
                            seq=record.seq,
                            event_type=record.event_type,
                            agent=record.agent,
                            payload=dict(record.payload or {}),
                            created_at=record.created_at,
                        )
                    )
                    if record.event_type in TERMINAL_EVENTS:
                        return

                if status is None or status.is_terminal:
                    return

                async for event in container.events.stream(
                    run_id,
                    after_seq=cursor,
                    idle_timeout=STREAM_IDLE_TIMEOUT_SECONDS,
                    heartbeat_limit=STREAM_HEARTBEATS_PER_RESYNC,
                ):
                    if event.event_type == "heartbeat":
                        yield ": keep-alive\n\n"
                        continue
                    cursor = max(cursor, event.seq)
                    yield _render(event)
                    if event.event_type in TERMINAL_EVENTS:
                        return
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            logger.info("sse_client_disconnected", run_id=run_id)
            raise
        finally:
            yield "event: stream.closed\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _action_out(action: Any) -> Any:
    from ...schemas.agent import OptimizationActionOut

    return OptimizationActionOut(
        id=action.id,
        run_id=action.run_id,
        campaign_id=action.campaign_id,
        creative_id=action.creative_id,
        action_type=action.action_type,
        status=action.status,
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


def _allocation_out(item: Any) -> Any:
    from ...schemas.agent import BudgetAllocationOut

    return BudgetAllocationOut(
        campaign_id=item.campaign_id,
        current_budget=item.current_budget,
        recommended_budget=item.recommended_budget,
        change_pct=item.change_pct,
        score=item.score,
        reason=item.reason,
        solver=item.solver,
    )
