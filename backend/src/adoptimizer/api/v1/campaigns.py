"""Campaign and creative endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ...core.deps import ClaimsDep, PaginationDep, SessionDep, require
from ...domain.enums import CampaignStatus, Permission, Platform
from ...schemas.api import (
    CampaignCreate,
    CampaignOut,
    CampaignUpdate,
    CreativeCreate,
    CreativeOut,
    CreativeStatusUpdate,
)
from ...schemas.common import Page
from ...services.audit import AuditService
from ...services.campaigns import CampaignService

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _service(session: SessionDep) -> CampaignService:
    return CampaignService(session)


@router.get(
    "",
    response_model=Page[CampaignOut],
    summary="List campaigns",
    dependencies=[require(Permission.CAMPAIGN_READ)],
)
async def list_campaigns(
    session: SessionDep,
    pagination: PaginationDep,
    platform: Annotated[Platform | None, Query()] = None,
    status_filter: Annotated[
        CampaignStatus | None, Query(alias="status", description="Filter by lifecycle status")
    ] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[CampaignOut]:
    """Paginated listing with platform, status and name filters."""
    rows, total = await _service(session).list_campaigns(
        pagination=pagination, platform=platform, status=status_filter, search=search
    )
    return Page[CampaignOut](
        items=[CampaignOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post(
    "",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a campaign",
    dependencies=[require(Permission.CAMPAIGN_WRITE)],
)
async def create_campaign(
    payload: CampaignCreate, claims: ClaimsDep, session: SessionDep
) -> CampaignOut:
    """Create a campaign and record the change in the audit trail."""
    campaign = await _service(session).create(
        payload.model_dump(exclude_none=True), actor_id=claims.subject
    )
    await AuditService(session).record(
        action="campaign.created",
        resource_type="campaign",
        resource_id=campaign.id,
        claims=claims,
        after={"name": campaign.name, "platform": campaign.platform},
    )
    await session.commit()
    return CampaignOut.model_validate(campaign)


@router.get(
    "/{campaign_id}",
    response_model=CampaignOut,
    summary="Fetch a campaign",
    dependencies=[require(Permission.CAMPAIGN_READ)],
)
async def get_campaign(campaign_id: str, session: SessionDep) -> CampaignOut:
    return CampaignOut.model_validate(await _service(session).get(campaign_id))


@router.patch(
    "/{campaign_id}",
    response_model=CampaignOut,
    summary="Update a campaign",
    dependencies=[require(Permission.CAMPAIGN_WRITE)],
)
async def update_campaign(
    campaign_id: str, payload: CampaignUpdate, claims: ClaimsDep, session: SessionDep
) -> CampaignOut:
    """Apply a partial update with before/after auditing."""
    service = _service(session)
    before = await service.get(campaign_id)
    snapshot_before = {"daily_budget": before.daily_budget, "status": before.status}

    campaign = await service.update(campaign_id, payload.model_dump(exclude_unset=True))
    await AuditService(session).record(
        action="campaign.updated",
        resource_type="campaign",
        resource_id=campaign_id,
        claims=claims,
        before=snapshot_before,
        after={"daily_budget": campaign.daily_budget, "status": campaign.status},
    )
    await session.commit()
    return CampaignOut.model_validate(campaign)


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a campaign",
    dependencies=[require(Permission.CAMPAIGN_WRITE)],
)
async def delete_campaign(campaign_id: str, claims: ClaimsDep, session: SessionDep) -> None:
    await _service(session).delete(campaign_id)
    await AuditService(session).record(
        action="campaign.deleted",
        resource_type="campaign",
        resource_id=campaign_id,
        claims=claims,
    )
    await session.commit()


@router.get(
    "/{campaign_id}/creatives",
    response_model=list[CreativeOut],
    summary="List a campaign's creatives",
    dependencies=[require(Permission.CAMPAIGN_READ)],
)
async def list_creatives(campaign_id: str, session: SessionDep) -> list[CreativeOut]:
    rows = await _service(session).creatives(campaign_id)
    return [CreativeOut.model_validate(row) for row in rows]


@router.post(
    "/{campaign_id}/creatives",
    response_model=CreativeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a creative",
    dependencies=[require(Permission.CREATIVE_WRITE)],
)
async def create_creative(
    campaign_id: str, payload: CreativeCreate, session: SessionDep
) -> CreativeOut:
    creative = await _service(session).add_creative(
        campaign_id, payload.model_dump(exclude={"campaign_id"})
    )
    await session.commit()
    return CreativeOut.model_validate(creative)


@router.patch(
    "/{campaign_id}/creatives/{creative_id}",
    response_model=CreativeOut,
    summary="Change a creative's status",
    dependencies=[require(Permission.CREATIVE_WRITE)],
)
async def update_creative_status(
    campaign_id: str, creative_id: str, payload: CreativeStatusUpdate, session: SessionDep
) -> CreativeOut:
    creative = await _service(session).set_creative_status(campaign_id, creative_id, payload.status)
    await session.commit()
    return CreativeOut.model_validate(creative)


@router.get(
    "/{campaign_id}/metrics",
    summary="Performance snapshot for a campaign",
    dependencies=[require(Permission.METRICS_READ)],
)
async def campaign_metrics(
    campaign_id: str,
    session: SessionDep,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    """Aggregated KPIs plus the per-creative breakdown."""
    service = _service(session)
    snapshots = await service.snapshots([campaign_id], days=days)
    breakdown = await service.creative_breakdown(campaign_id, days=days)
    return {
        "campaign_id": campaign_id,
        "window_days": days,
        "snapshot": snapshots[0].model_dump() if snapshots else None,
        "creatives": breakdown,
    }
