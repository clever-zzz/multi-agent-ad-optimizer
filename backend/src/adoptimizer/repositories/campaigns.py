"""Campaign, creative and daily-metric persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, select

from ..core.clock import utc_today
from ..core.ids import new_id
from ..domain.enums import CampaignStatus, CreativeStatus, Platform
from ..domain.kpi import PerformanceSnapshot
from ..infra.db.models import Campaign, Creative, DailyMetric
from .base import BaseRepository


class CampaignRepository(BaseRepository[Campaign]):
    model = Campaign
    resource_name = "campaign"

    async def create(
        self,
        *,
        name: str,
        platform: Platform,
        daily_budget: float,
        total_budget: float,
        target_cpa: float,
        target_roas: float,
        start_date: date,
        end_date: date | None = None,
        objective: str = "conversions",
        target_audience: str = "",
        external_id: str | None = None,
        status: CampaignStatus = CampaignStatus.ACTIVE,
        created_by: str | None = None,
    ) -> Campaign:
        """Insert a campaign."""
        campaign = Campaign(
            id=new_id("camp"),
            name=name,
            platform=platform.value,
            status=status.value,
            external_id=external_id,
            daily_budget=daily_budget,
            total_budget=total_budget,
            target_cpa=target_cpa,
            target_roas=target_roas,
            start_date=start_date,
            end_date=end_date,
            objective=objective,
            target_audience=target_audience,
            created_by=created_by,
        )
        return await self.add(campaign)

    async def active(self, *, limit: int = 500) -> list[Campaign]:
        """Every campaign currently eligible for optimization."""
        statement = (
            select(Campaign)
            .where(Campaign.status == CampaignStatus.ACTIVE.value)
            .order_by(Campaign.name.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def by_ids(self, campaign_ids: Sequence[str]) -> list[Campaign]:
        """Fetch a specific set of campaigns."""
        if not campaign_ids:
            return []
        statement = select(Campaign).where(Campaign.id.in_(list(campaign_ids)))
        return list((await self.session.execute(statement)).scalars().all())

    async def daily_budgets(self, campaign_ids: Sequence[str] | None = None) -> dict[str, float]:
        """Map campaign id to its daily budget for burn-rate alerts."""
        statement: Select[Any] = select(Campaign.id, Campaign.daily_budget)
        if campaign_ids:
            statement = statement.where(Campaign.id.in_(list(campaign_ids)))
        rows = (await self.session.execute(statement)).all()
        return {str(row[0]): float(row[1] or 0.0) for row in rows}

    async def targets(
        self, campaign_ids: Sequence[str] | None = None
    ) -> dict[str, dict[str, float]]:
        """Map campaign id to its target CPA and ROAS."""
        statement: Select[Any] = select(Campaign.id, Campaign.target_cpa, Campaign.target_roas)
        if campaign_ids:
            statement = statement.where(Campaign.id.in_(list(campaign_ids)))
        rows = (await self.session.execute(statement)).all()
        return {
            str(row[0]): {"target_cpa": float(row[1] or 0.0), "target_roas": float(row[2] or 0.0)}
            for row in rows
        }


class CreativeRepository(BaseRepository[Creative]):
    model = Creative
    resource_name = "creative"

    async def create(
        self,
        *,
        campaign_id: str,
        headline: str,
        description: str = "",
        cta_text: str = "Learn More",
        creative_type: str = "text",
        target_emotion: str = "",
        ab_group: str = "control",
        origin: str = "human",
        status: CreativeStatus = CreativeStatus.DRAFT,
        generated_by_run_id: str | None = None,
    ) -> Creative:
        """Insert a creative."""
        creative = Creative(
            id=new_id("cre"),
            campaign_id=campaign_id,
            headline=headline,
            description=description,
            cta_text=cta_text,
            creative_type=creative_type,
            target_emotion=target_emotion,
            ab_group=ab_group,
            origin=origin,
            status=status.value,
            generated_by_run_id=generated_by_run_id,
        )
        return await self.add(creative)

    async def for_campaign(self, campaign_id: str, *, limit: int = 200) -> list[Creative]:
        """Creatives belonging to one campaign."""
        statement = (
            select(Creative)
            .where(Creative.campaign_id == campaign_id)
            .order_by(Creative.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def by_ids(self, creative_ids: Sequence[str]) -> list[Creative]:
        if not creative_ids:
            return []
        statement = select(Creative).where(Creative.id.in_(list(creative_ids)))
        return list((await self.session.execute(statement)).scalars().all())


class MetricRepository(BaseRepository[DailyMetric]):
    model = DailyMetric
    resource_name = "daily_metric"

    async def upsert_daily(
        self,
        *,
        campaign_id: str,
        stat_date: date,
        impressions: int,
        clicks: int,
        conversions: int,
        cost: float,
        revenue: float,
        creative_id: str | None = None,
        unique_reach: int = 0,
    ) -> DailyMetric:
        """Insert or update one daily aggregate slot."""
        statement = select(DailyMetric).where(
            DailyMetric.campaign_id == campaign_id,
            DailyMetric.stat_date == stat_date,
            DailyMetric.creative_id.is_(None)
            if creative_id is None
            else DailyMetric.creative_id == creative_id,
        )
        existing = (await self.session.execute(statement)).scalar_one_or_none()

        if existing is None:
            record = DailyMetric(
                id=new_id("met"),
                campaign_id=campaign_id,
                creative_id=creative_id,
                stat_date=stat_date,
                impressions=impressions,
                clicks=clicks,
                conversions=conversions,
                cost=cost,
                revenue=revenue,
                unique_reach=unique_reach,
            )
            return await self.add(record)

        existing.impressions = impressions
        existing.clicks = clicks
        existing.conversions = conversions
        existing.cost = cost
        existing.revenue = revenue
        existing.unique_reach = unique_reach
        await self.flush()
        return existing

    async def snapshots(
        self, campaign_ids: Sequence[str] | None = None, *, days: int = 7
    ) -> list[PerformanceSnapshot]:
        """Aggregate daily rows into per-campaign performance snapshots."""
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        statement: Select[Any] = (
            select(
                DailyMetric.campaign_id,
                Campaign.name.label("campaign_name"),
                func.sum(DailyMetric.impressions).label("impressions"),
                func.sum(DailyMetric.clicks).label("clicks"),
                func.sum(DailyMetric.conversions).label("conversions"),
                func.sum(DailyMetric.cost).label("cost"),
                func.sum(DailyMetric.revenue).label("revenue"),
            )
            .join(Campaign, Campaign.id == DailyMetric.campaign_id)
            .where(DailyMetric.stat_date >= cutoff)
            .group_by(DailyMetric.campaign_id, Campaign.name)
        )
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(list(campaign_ids)))

        rows = (await self.session.execute(statement)).all()
        snapshots: list[PerformanceSnapshot] = []
        for row in rows:
            impressions = int(row.impressions or 0)
            clicks = min(int(row.clicks or 0), impressions)
            conversions = min(int(row.conversions or 0), clicks)
            snapshots.append(
                PerformanceSnapshot(
                    campaign_id=str(row.campaign_id),
                    campaign_name=str(row.campaign_name or ""),
                    impressions=impressions,
                    clicks=clicks,
                    conversions=conversions,
                    total_cost=float(row.cost or 0.0),
                    total_revenue=float(row.revenue or 0.0),
                )
            )
        return snapshots

    async def creative_stats(
        self, campaign_ids: Sequence[str] | None = None, *, days: int = 7
    ) -> dict[str, list[dict[str, Any]]]:
        """Per-creative aggregates grouped by campaign, for creative scoring."""
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        statement: Select[Any] = (
            select(
                DailyMetric.campaign_id,
                DailyMetric.creative_id,
                func.sum(DailyMetric.impressions).label("impressions"),
                func.sum(DailyMetric.clicks).label("clicks"),
                func.sum(DailyMetric.conversions).label("conversions"),
                func.sum(DailyMetric.cost).label("cost"),
                func.sum(DailyMetric.revenue).label("revenue"),
            )
            .where(
                and_(
                    DailyMetric.stat_date >= cutoff,
                    DailyMetric.creative_id.is_not(None),
                )
            )
            .group_by(DailyMetric.campaign_id, DailyMetric.creative_id)
        )
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(list(campaign_ids)))

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in (await self.session.execute(statement)).all():
            grouped.setdefault(str(row.campaign_id), []).append(
                {
                    "creative_id": str(row.creative_id),
                    "impressions": int(row.impressions or 0),
                    "clicks": int(row.clicks or 0),
                    "conversions": int(row.conversions or 0),
                    "cost": float(row.cost or 0.0),
                    "revenue": float(row.revenue or 0.0),
                }
            )
        return grouped

    async def reach(
        self, campaign_ids: Sequence[str] | None = None, *, days: int = 7
    ) -> dict[str, int]:
        """Unique reach per campaign, used by the frequency-fatigue rule."""
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        statement: Select[Any] = (
            select(DailyMetric.campaign_id, func.max(DailyMetric.unique_reach))
            .where(DailyMetric.stat_date >= cutoff, DailyMetric.unique_reach > 0)
            .group_by(DailyMetric.campaign_id)
        )
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(list(campaign_ids)))
        return {
            str(row[0]): int(row[1] or 0) for row in (await self.session.execute(statement)).all()
        }
