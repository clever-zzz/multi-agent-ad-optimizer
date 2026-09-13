"""Campaign and creative administration, plus optimizer input assembly."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utc_today
from ..core.errors import ConflictError, NotFoundError, ValidationFailure
from ..core.logging import get_logger
from ..domain.enums import CampaignStatus, CreativeStatus, Platform
from ..domain.kpi import PerformanceSnapshot
from ..infra.db.models import Campaign, Creative
from ..repositories.campaigns import CampaignRepository, CreativeRepository, MetricRepository
from ..schemas.common import PaginationParams, SortOrder

logger = get_logger(__name__)

MAX_CAMPAIGNS_PER_RUN = 500


class CampaignService:
    """Use cases around campaign and creative lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        self._campaigns = CampaignRepository(session)
        self._creatives = CreativeRepository(session)
        self._metrics = MetricRepository(session)

    async def create(self, payload: dict[str, Any], *, actor_id: str | None = None) -> Campaign:
        """Create a campaign, validating platform and budget sanity."""
        try:
            platform = Platform(str(payload["platform"]))
        except ValueError as exc:
            raise ValidationFailure(
                "Unsupported platform: " + str(payload.get("platform"))
            ) from exc

        daily = float(payload.get("daily_budget", 0) or 0)
        total = float(payload.get("total_budget", 0) or 0)
        if daily <= 0:
            raise ValidationFailure("daily_budget must be greater than zero")
        if total and total < daily:
            raise ValidationFailure("total_budget cannot be smaller than daily_budget")

        start = payload.get("start_date") or utc_today()
        end = payload.get("end_date")
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        if end and end < start:
            raise ValidationFailure("end_date cannot precede start_date")

        return await self._campaigns.create(
            name=str(payload["name"]).strip(),
            platform=platform,
            daily_budget=daily,
            total_budget=total or daily * 30,
            target_cpa=float(payload.get("target_cpa", 100.0) or 100.0),
            target_roas=float(payload.get("target_roas", 2.0) or 2.0),
            start_date=start,
            end_date=end,
            objective=str(payload.get("objective", "conversions")),
            target_audience=str(payload.get("target_audience", "")),
            external_id=payload.get("external_id"),
            created_by=actor_id,
        )

    # Not called `list`: a method with that name shadows the builtin inside the
    # rest of the class body, so every later `-> list[X]` annotation resolves to
    # the method and mypy rejects it as a type.
    async def list_campaigns(
        self,
        *,
        pagination: PaginationParams,
        platform: Platform | None = None,
        status: CampaignStatus | None = None,
        search: str | None = None,
    ) -> tuple[list[Campaign], int]:
        """Paginated, filterable campaign listing."""
        filters = []
        if platform is not None:
            filters.append(Campaign.platform == platform.value)
        if status is not None:
            filters.append(Campaign.status == status.value)
        if search:
            filters.append(Campaign.name.ilike("%" + search.strip() + "%"))

        return await self._campaigns.list(
            filters=filters,
            pagination=pagination,
            order_by=Campaign.created_at,
            order=SortOrder.DESC,
        )

    async def get(self, campaign_id: str) -> Campaign:
        return await self._campaigns.get_or_raise(campaign_id)

    async def update(self, campaign_id: str, payload: dict[str, Any]) -> Campaign:
        """Apply a partial update, rejecting contradictory values."""
        campaign = await self._campaigns.get_or_raise(campaign_id)

        if "status" in payload:
            try:
                campaign.status = CampaignStatus(str(payload["status"])).value
            except ValueError as exc:
                raise ValidationFailure("Unknown campaign status") from exc

        if "daily_budget" in payload:
            daily = float(payload["daily_budget"] or 0)
            if daily <= 0:
                raise ValidationFailure("daily_budget must be greater than zero")
            campaign.daily_budget = daily

        if "total_budget" in payload:
            campaign.total_budget = float(payload["total_budget"] or 0)

        if campaign.total_budget and campaign.total_budget < campaign.daily_budget:
            raise ConflictError("total_budget cannot be smaller than daily_budget")

        for field in ("name", "target_audience", "objective", "external_id"):
            if field in payload:
                setattr(campaign, field, payload[field])

        for field in ("target_cpa", "target_roas"):
            if field in payload:
                value = float(payload[field] or 0)
                if value <= 0:
                    raise ValidationFailure(field + " must be greater than zero")
                setattr(campaign, field, value)

        if "end_date" in payload:
            end = payload["end_date"]
            campaign.end_date = date.fromisoformat(end) if isinstance(end, str) else end
            if campaign.end_date and campaign.end_date < campaign.start_date:
                raise ValidationFailure("end_date cannot precede start_date")

        await self._campaigns.flush()
        return campaign

    async def delete(self, campaign_id: str) -> None:
        campaign = await self._campaigns.get_or_raise(campaign_id)
        await self._campaigns.delete(campaign)
        logger.info("campaign_deleted", campaign_id=campaign_id)

    async def add_creative(self, campaign_id: str, payload: dict[str, Any]) -> Creative:
        """Attach a creative to a campaign."""
        await self._campaigns.get_or_raise(campaign_id)
        headline = str(payload.get("headline", "")).strip()
        if len(headline) < 4:
            raise ValidationFailure("headline must be at least 4 characters")

        return await self._creatives.create(
            campaign_id=campaign_id,
            headline=headline[:300],
            description=str(payload.get("description", ""))[:2000],
            cta_text=str(payload.get("cta_text", "Learn More"))[:60],
            creative_type=str(payload.get("creative_type", "text")),
            target_emotion=str(payload.get("target_emotion", "")),
            ab_group=str(payload.get("ab_group", "control")),
            origin=str(payload.get("origin", "human")),
            status=CreativeStatus(str(payload.get("status", "draft"))),
        )

    async def creatives(self, campaign_id: str) -> list[Creative]:
        return await self._creatives.for_campaign(campaign_id)

    async def set_creative_status(
        self, campaign_id: str, creative_id: str, status: CreativeStatus
    ) -> Creative:
        """Change a creative's status, scoped to the campaign named in the path.

        The ownership check is what stops a caller holding ``creative:write``
        from reaching a creative that belongs to some other campaign simply by
        naming an arbitrary campaign in the URL. A mismatch answers 404 rather
        than 403 so the endpoint never confirms that the creative exists.
        """
        creative = await self._creatives.get_or_raise(creative_id)
        if creative.campaign_id != campaign_id:
            raise NotFoundError(
                "Creative " + creative_id + " does not belong to campaign " + campaign_id
            )
        creative.status = status.value
        await self._creatives.flush()
        return creative

    async def daily_budgets(self, campaign_ids: Sequence[str] | None = None) -> dict[str, float]:
        """Daily budget per campaign, used by the burn-rate alert rule."""
        return await self._campaigns.daily_budgets(campaign_ids)

    async def snapshots(
        self, campaign_ids: Sequence[str] | None = None, *, days: int = 7
    ) -> list[PerformanceSnapshot]:
        """Performance snapshots from the primary datastore."""
        return await self._metrics.snapshots(campaign_ids, days=days)

    async def creative_breakdown(self, campaign_id: str, *, days: int = 7) -> list[dict[str, Any]]:
        """Per-creative aggregates for one campaign, for the detail view."""
        stats = await self._metrics.creative_stats([campaign_id], days=days)
        return stats.get(campaign_id, [])

    async def collect_run_inputs(
        self, campaign_ids: Sequence[str] | None, *, days: int = 7
    ) -> dict[str, Any]:
        """Assemble everything the agents need in one pass.

        Gathering these up front keeps the agent loop free of database access,
        which is what allows a run to execute inside a background task with its
        own session instead of holding a request-scoped one.
        """
        ids = list(campaign_ids or [])
        if not ids:
            active = await self._campaigns.active(limit=MAX_CAMPAIGNS_PER_RUN)
            ids = [campaign.id for campaign in active]
        if not ids:
            raise NotFoundError("No active campaigns are available to optimize")

        snapshots = await self._metrics.snapshots(ids, days=days)
        if not snapshots:
            raise ConflictError(
                "The selected campaigns have no delivery data in the last " + str(days) + " days"
            )

        return {
            "campaign_ids": ids,
            "snapshots": snapshots,
            # The identity bridge agents need to call platform tools: internal id
            # in, network coordinates out. Collected here rather than inside the
            # loop so a run performs one query no matter how many iterations it
            # takes, and so agents stay free of database access.
            "campaign_refs": await self._campaigns.refs(ids),
            "daily_budgets": await self._campaigns.daily_budgets(ids),
            "campaign_targets": await self._campaigns.targets(ids),
            "creative_stats": await self._metrics.creative_stats(ids, days=days),
            "reach": await self._metrics.reach(ids, days=days),
            "existing_creatives": await self._existing_creative_map(ids),
        }

    async def _existing_creative_map(
        self, campaign_ids: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Merge creative metadata with its performance stats."""
        stats = await self._metrics.creative_stats(campaign_ids, days=30)
        result: dict[str, list[dict[str, Any]]] = {}

        for campaign_id in campaign_ids:
            creatives = await self._creatives.for_campaign(campaign_id)
            stats_by_id = {row["creative_id"]: row for row in stats.get(campaign_id, [])}
            entries: list[dict[str, Any]] = []
            for creative in creatives:
                row = stats_by_id.get(creative.id, {})
                entries.append(
                    {
                        "creative_id": creative.id,
                        "headline": creative.headline,
                        "status": creative.status,
                        "ab_group": creative.ab_group,
                        "origin": creative.origin,
                        "impressions": int(row.get("impressions", 0)),
                        "clicks": int(row.get("clicks", 0)),
                        "conversions": int(row.get("conversions", 0)),
                        "cost": float(row.get("cost", 0.0)),
                        "revenue": float(row.get("revenue", 0.0)),
                    }
                )
            result[campaign_id] = entries
        return result
