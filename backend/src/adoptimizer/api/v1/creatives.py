"""Cross-campaign creative catalogue.

The per-campaign route answers "what does this campaign have". This one answers
"what exists across the account, and how much of it did the model write", which
is what the creative review queue and the model-output audit need. Both are
served from the same table so the two views can never disagree.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from ...core.deps import PaginationDep, SessionDep, require
from ...domain.enums import CreativeStatus, Permission
from ...infra.db.models import Creative
from ...repositories.campaigns import CreativeRepository
from ...schemas.api import CreativeOut
from ...schemas.common import Page, SortOrder

router = APIRouter(prefix="/creatives", tags=["creatives"])

SORTABLE_COLUMNS: dict[str, Any] = {
    "created_at": Creative.created_at,
    "score": Creative.score,
    "headline": Creative.headline,
    "status": Creative.status,
}


def _filters(
    *,
    campaign_id: str | None,
    status_filter: CreativeStatus | None,
    origin: str | None,
    ab_group: str | None,
    creative_type: str | None,
    search: str | None,
) -> list[Any]:
    conditions: list[Any] = []
    if campaign_id:
        conditions.append(Creative.campaign_id == campaign_id)
    if status_filter is not None:
        conditions.append(Creative.status == status_filter.value)
    if origin:
        conditions.append(Creative.origin == origin)
    if ab_group:
        conditions.append(Creative.ab_group == ab_group)
    if creative_type:
        conditions.append(Creative.creative_type == creative_type)
    if search:
        needle = "%" + search.strip() + "%"
        conditions.append(or_(Creative.headline.ilike(needle), Creative.description.ilike(needle)))
    return conditions


@router.get(
    "/summary",
    summary="Creative counts by origin and status",
    dependencies=[require(Permission.CAMPAIGN_READ)],
)
async def creative_summary(session: SessionDep) -> dict[str, Any]:
    """Totals that head the review queue, including model-authored share."""
    origin_rows = (
        await session.execute(
            select(Creative.origin, func.count(Creative.id)).group_by(Creative.origin)
        )
    ).all()
    status_rows = (
        await session.execute(
            select(Creative.status, func.count(Creative.id)).group_by(Creative.status)
        )
    ).all()

    by_origin = {str(row[0]): int(row[1]) for row in origin_rows}
    by_status = {str(row[0]): int(row[1]) for row in status_rows}
    total = sum(by_origin.values())
    generated = sum(count for key, count in by_origin.items() if key != "human")

    return {
        "total": total,
        "by_origin": by_origin,
        "by_status": by_status,
        "generated": generated,
        "generated_share": round(generated / total, 4) if total else 0.0,
        "scored": int(
            await session.scalar(select(func.count(Creative.id)).where(Creative.score.is_not(None)))
            or 0
        ),
    }


@router.get(
    "",
    response_model=Page[CreativeOut],
    summary="List creatives across every campaign",
    dependencies=[require(Permission.CAMPAIGN_READ)],
)
async def list_creatives(
    session: SessionDep,
    pagination: PaginationDep,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
    status_filter: Annotated[CreativeStatus | None, Query(alias="status")] = None,
    origin: Annotated[str | None, Query(max_length=20)] = None,
    ab_group: Annotated[str | None, Query(max_length=40)] = None,
    creative_type: Annotated[str | None, Query(alias="type", max_length=20)] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    sort: Annotated[str, Query(pattern="^(created_at|score|headline|status)$")] = "created_at",
    order: SortOrder = SortOrder.DESC,
) -> Page[CreativeOut]:
    """Paginated, filterable and sortable creative listing."""
    rows, total = await CreativeRepository(session).list(
        filters=_filters(
            campaign_id=campaign_id,
            status_filter=status_filter,
            origin=origin,
            ab_group=ab_group,
            creative_type=creative_type,
            search=search,
        ),
        pagination=pagination,
        # NULL scores sort last regardless of direction so unscored drafts do not
        # masquerade as either the best or the worst creative.
        order_by=func.coalesce(SORTABLE_COLUMNS[sort], 0.0)
        if sort == "score"
        else SORTABLE_COLUMNS[sort],
        order=order,
    )
    return Page[CreativeOut](
        items=[CreativeOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
