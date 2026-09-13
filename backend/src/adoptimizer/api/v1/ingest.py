"""Metric ingestion endpoints.

Two halves of one job. ``POST /ingest/metrics`` is the push door: a warehouse
export, a commerce webhook or a backfill script hands us rows and gets back an
accounting of every one of them. ``GET /ingest/batches``, ``GET /ingest/sources``
and ``GET /ingest/schedule`` are the observability half, because a door nobody can
inspect is a door nobody will trust.

Pulling is not exposed over HTTP. A pull takes as long as the platform takes to
answer, which is not a thing to hold a request open for; it belongs to the CLI
and to the scheduler, both of which can report progress and retry. ``GET
/ingest/schedule`` therefore *plans* without pulling - it answers "is this feed
due, covered, catching up, or owed a manual backfill" and is cheap enough to poll
from a dashboard.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from ...core.deps import (
    ClaimsDep,
    ContainerDep,
    PaginationDep,
    SessionDep,
    client_metadata,
    require,
)
from ...domain.enums import Permission
from ...infra.db.models import IngestBatch
from ...repositories.ingest import IngestBatchRepository
from ...schemas.common import Page
from ...schemas.ingest import (
    IngestBatchIn,
    IngestBatchOut,
    IngestReportOut,
    IngestSourceOut,
    IngestSourceStatusOut,
)
from ...schemas.scheduling import SchedulerStatusOut
from ...services.ingest import IngestService
from ...services.scheduling import IngestScheduler

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post(
    "/metrics",
    response_model=IngestReportOut,
    summary="Push daily metric records",
    dependencies=[require(Permission.METRICS_WRITE)],
)
async def push_metrics(
    payload: IngestBatchIn, request: Request, claims: ClaimsDep, session: SessionDep
) -> IngestReportOut:
    """Ingest one batch of asserted daily aggregates.

    Returns 200 even when individual records were rejected or could not be
    attributed: the batch was processed and the report says what happened to each
    row. A 4xx here means the request itself was unusable, which is a different
    problem and should not be conflated with a partially dirty feed.
    """
    report = await IngestService(session).ingest(
        payload.records,
        source=payload.source,
        dry_run=payload.dry_run,
        actor=claims.email,
        claims=claims,
        client=client_metadata(request),
    )
    await session.commit()
    return report


@router.get(
    "/batches",
    response_model=Page[IngestBatchOut],
    summary="List ingestion attempts",
    dependencies=[require(Permission.METRICS_READ)],
)
async def list_batches(
    session: SessionDep,
    pagination: PaginationDep,
    source: Annotated[str | None, Query(max_length=30)] = None,
) -> Page[IngestBatchOut]:
    """Newest attempts first, optionally narrowed to one feed."""
    filters = [IngestBatch.source == source] if source else []
    rows, total = await IngestBatchRepository(session).list(
        filters=filters, pagination=pagination, order_by=IngestBatch.created_at
    )
    return Page[IngestBatchOut](
        items=[IngestBatchOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/sources",
    response_model=IngestSourceStatusOut,
    summary="Registered metric feeds",
    dependencies=[require(Permission.METRICS_READ)],
)
async def list_sources(container: ContainerDep) -> IngestSourceStatusOut:
    """Which feeds exist and which could be pulled right now.

    A source is listed even when unconfigured, so "installed but not
    credentialed" is never mistaken for "not implemented".
    """
    registry = container.ingest_sources
    return IngestSourceStatusOut(
        sources=[
            IngestSourceOut(name=name, registered=True, configured=bool(info.get("configured")))
            for name, info in registry.status().items()
        ],
        configured=registry.configured(),
    )


@router.get(
    "/schedule",
    response_model=SchedulerStatusOut,
    summary="Scheduled pull status",
    dependencies=[require(Permission.METRICS_READ)],
)
async def get_schedule(container: ContainerDep) -> SchedulerStatusOut:
    """Whether each configured feed is due, covered, catching up or owed a backfill.

    The per-feed ``plan.reason`` is the answer to "why did the numbers not
    update": ``covered`` means it already ran, ``catchup`` means it is closing a
    gap, and ``capped`` means the gap is wider than ``INGEST__MAX_CATCHUP_DAYS``
    and the ``plan.detail`` string carries the exact backfill command to run.
    ``lease.held`` says whether something is pulling right now.

    Read-only: it plans without pulling, so polling it cannot start work.
    """
    scheduler = IngestScheduler(
        container.database.session_factory,
        container.ingest_sources,
        container.settings.ingest,
    )
    return await scheduler.status()
