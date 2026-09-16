"""Campaign, creative and daily-metric persistence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, select

from ..core.clock import utc_today
from ..core.ids import new_id
from ..core.logging import get_logger
from ..domain.enums import CampaignStatus, CreativeStatus, Platform
from ..domain.kpi import PerformanceSnapshot, reconcile_delivery
from ..infra.analytics import DailyMetricRow
from ..infra.db.models import Campaign, Creative, DailyMetric
from .base import BaseRepository

logger = get_logger(__name__)

# The measures a snapshot folds up. ``unique_reach`` is deliberately absent:
# reach is not additive across days, so summing it would be meaningless.
MEASURE_COLUMNS = ("impressions", "clicks", "conversions", "cost", "revenue")


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

    async def syncable(self, *, limit: int = 500) -> list[Campaign]:
        """Campaigns a feed can name: those carrying a platform external id.

        A campaign with no external id cannot be addressed by any platform report,
        so it is not a pull target. It is still reachable by internal id, which is
        why it is excluded here rather than flagged.
        """
        statement = (
            select(Campaign)
            .where(Campaign.external_id.is_not(None), Campaign.external_id != "")
            .order_by(Campaign.name.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def ids_by_external(self, pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Resolve (platform, external_id) to the internal campaign id.

        One query for the whole batch. The ``IN`` lists form a cross product, so
        the result is filtered back down to the pairs actually asked about;
        ``uq_campaign_platform_external`` guarantees at most one campaign per
        pair, which is what makes the mapping a dict rather than a list.
        """
        if not pairs:
            return {}
        wanted = {(platform, external_id) for platform, external_id in pairs}
        statement = select(Campaign.platform, Campaign.external_id, Campaign.id).where(
            Campaign.platform.in_(sorted({platform for platform, _ in wanted})),
            Campaign.external_id.in_(sorted({external for _, external in wanted})),
        )
        rows = (await self.session.execute(statement)).all()
        found = {(str(row[0]), str(row[1])): str(row[2]) for row in rows}
        return {pair: found[pair] for pair in wanted if pair in found}

    async def existing_ids(self, campaign_ids: Sequence[str]) -> set[str]:
        """Which of these internal ids actually exist."""
        if not campaign_ids:
            return set()
        statement = select(Campaign.id).where(Campaign.id.in_(list(campaign_ids)))
        return {str(row[0]) for row in (await self.session.execute(statement)).all()}

    async def refs(self, campaign_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Map internal campaign id to the coordinates a tool call needs.

        Agents reason in internal ids because that is what the warehouse and the
        proposal tables use; ad networks only recognise their own external ids.
        Resolving that bridge once per run, here, is what lets an agent call a
        tool without being handed database access. A campaign with no external id
        is reported with an empty string rather than dropped, so the caller can
        tell "not synced yet" apart from "not in this run".
        """
        return {
            campaign.id: {
                "platform": campaign.platform,
                "external_id": campaign.external_id or "",
                "name": campaign.name,
                "status": campaign.status,
            }
            for campaign in await self.by_ids(campaign_ids)
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

    async def campaign_by_id(self, creative_ids: Sequence[str]) -> dict[str, str]:
        """Map creative id to the campaign that owns it.

        Ingestion needs the owner, not just existence: a metric row that names a
        creative belonging to a different campaign would put spend under one
        campaign and its breakdown under another, and both totals would then be
        wrong in a way no single query reveals.
        """
        if not creative_ids:
            return {}
        statement = select(Creative.id, Creative.campaign_id).where(
            Creative.id.in_(list(creative_ids))
        )
        return {str(row[0]): str(row[1]) for row in (await self.session.execute(statement)).all()}


class MetricRepository(BaseRepository[DailyMetric]):
    model = DailyMetric
    resource_name = "daily_metric"

    async def upsert_daily(
        self,
        *,
        campaign_id: str,
        stat_date: date,
        impressions: int | None = None,
        clicks: int | None = None,
        conversions: int | None = None,
        cost: float | None = None,
        revenue: float | None = None,
        creative_id: str | None = None,
        unique_reach: int | None = None,
        source: str | None = None,
        batch_id: str | None = None,
    ) -> DailyMetric:
        """Insert or update one daily aggregate slot.

        ``None`` never overwrites. On an insert it becomes the column default; on
        an update the stored value is left alone. That single rule is what lets
        feeds that measure different things share one table: an ad network can
        assert cost without erasing the revenue a commerce feed supplied, and a
        genuine zero still lands as a zero because zero is not ``None``.

        ``source`` and ``batch_id`` are provenance, not identity: they never take
        part in locating the slot, they only record who last asserted it, and
        they follow the same rule so an internal correction cannot wipe the stamp
        of the feed that originally delivered the number.
        """
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
                impressions=0 if impressions is None else impressions,
                clicks=0 if clicks is None else clicks,
                conversions=0 if conversions is None else conversions,
                cost=0.0 if cost is None else cost,
                revenue=0.0 if revenue is None else revenue,
                unique_reach=0 if unique_reach is None else unique_reach,
                source=source,
                batch_id=batch_id,
            )
            return await self.add(record)

        supplied: dict[str, Any] = {
            "impressions": impressions,
            "clicks": clicks,
            "conversions": conversions,
            "cost": cost,
            "revenue": revenue,
            "unique_reach": unique_reach,
            "source": source,
            "batch_id": batch_id,
        }
        for column, value in supplied.items():
            if value is not None:
                setattr(existing, column, value)
        await self.flush()
        return existing

    async def existing_slots(
        self, campaign_ids: Sequence[str], dates: Sequence[date]
    ) -> set[tuple[str, str | None, date]]:
        """Which (campaign, creative, day) slots already hold a row.

        One query instead of one per record, so an ingestion batch can report how
        many slots it created versus overwrote without paying for a SELECT each.
        """
        if not campaign_ids or not dates:
            return set()
        statement = select(
            DailyMetric.campaign_id, DailyMetric.creative_id, DailyMetric.stat_date
        ).where(
            DailyMetric.campaign_id.in_(list(campaign_ids)),
            DailyMetric.stat_date.in_(list(dates)),
        )
        return {(row[0], row[1], row[2]) for row in (await self.session.execute(statement)).all()}

    async def export_daily(
        self, *, days: int, campaign_ids: Sequence[str] | None = None
    ) -> list[DailyMetricRow]:
        """Every stored slot in the window, for mirroring into the warehouse.

        Unlike ``snapshots``, this deliberately does *not* merge the two
        granularities. The warehouse stores the campaign roll-up and the
        creative breakdown as separate rows and its own reader decides which to
        use; collapsing them here would silently discard the creative detail on
        the way out, and the mirror would then be a lossy copy of the source.
        """
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        statement = select(DailyMetric).where(DailyMetric.stat_date >= cutoff)
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(list(campaign_ids)))
        rows = (await self.session.execute(statement)).scalars().all()
        return [
            DailyMetricRow(
                campaign_id=row.campaign_id,
                stat_date=row.stat_date,
                impressions=int(row.impressions or 0),
                clicks=int(row.clicks or 0),
                conversions=int(row.conversions or 0),
                cost=float(row.cost or 0.0),
                revenue=float(row.revenue or 0.0),
                creative_id=row.creative_id or "",
                unique_reach=row.unique_reach,
                source=row.source or "",
            )
            for row in rows
        ]

    async def _daily_slots(
        self, cutoff: date, campaign_ids: Sequence[str] | None, *, breakdown: bool
    ) -> dict[tuple[str, date], dict[str, Any]]:
        """Delivery per (campaign, day) from exactly one of the two granularities."""
        granularity = (
            DailyMetric.creative_id.is_not(None) if breakdown else DailyMetric.creative_id.is_(None)
        )
        statement: Select[Any] = (
            select(
                DailyMetric.campaign_id,
                DailyMetric.stat_date,
                Campaign.name.label("campaign_name"),
                func.sum(DailyMetric.impressions).label("impressions"),
                func.sum(DailyMetric.clicks).label("clicks"),
                func.sum(DailyMetric.conversions).label("conversions"),
                func.sum(DailyMetric.cost).label("cost"),
                func.sum(DailyMetric.revenue).label("revenue"),
            )
            .join(Campaign, Campaign.id == DailyMetric.campaign_id)
            .where(DailyMetric.stat_date >= cutoff, granularity)
            .group_by(DailyMetric.campaign_id, DailyMetric.stat_date, Campaign.name)
        )
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(list(campaign_ids)))

        return {
            (str(row.campaign_id), row.stat_date): {
                "campaign_name": str(row.campaign_name or ""),
                **{column: float(getattr(row, column) or 0.0) for column in MEASURE_COLUMNS},
            }
            for row in (await self.session.execute(statement)).all()
        }

    async def merged_slots(
        self, cutoff: date, campaign_ids: Sequence[str] | None = None
    ) -> dict[tuple[str, date], dict[str, Any]]:
        """Delivery per (campaign, day) from exactly one of the two granularities.

        The roll-up wins a bucket that has one; the breakdown only fills buckets
        that have no roll-up at all. Merging per bucket rather than per campaign is
        what keeps a mixed portfolio exact - a campaign ingested only per creative
        still contributes its delivery instead of reading as zero and disappearing
        from whatever is being computed.

        Public because every reader of this table needs the same answer, not only
        the snapshot path. This is the primary-datastore form of the rule
        ``ClickHouseWarehouse._delivery_relation`` applies over the mirror and
        ``SqlAggregateWarehouse._merged_slots`` applies over its own copy.
        """
        slots = await self._daily_slots(cutoff, campaign_ids, breakdown=False)
        breakdown = await self._daily_slots(cutoff, campaign_ids, breakdown=True)
        for key, measures in breakdown.items():
            slots.setdefault(key, measures)
        return slots

    async def snapshots(
        self, campaign_ids: Sequence[str] | None = None, *, days: int = 7
    ) -> list[PerformanceSnapshot]:
        """Aggregate daily rows into per-campaign performance snapshots.

        Delivery comes from ``merged_slots``, the one implementation of the
        two-granularity rule on this side of the warehouse. Summing the table
        directly would add each campaign-level roll-up to the creative breakdown of
        that same roll-up and inflate every KPI - and this is the reader that feeds
        ``collect_run_inputs``, so the inflated numbers are what every agent in a
        run scores on.
        """
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        slots = await self.merged_slots(cutoff, campaign_ids)

        names: dict[str, str] = {}
        totals: dict[str, dict[str, float]] = {}
        for (campaign_id, _day), measures in slots.items():
            names.setdefault(campaign_id, str(measures["campaign_name"]))
            bucket = totals.setdefault(campaign_id, dict.fromkeys(MEASURE_COLUMNS, 0.0))
            for column in MEASURE_COLUMNS:
                bucket[column] += float(measures[column])

        snapshots: list[PerformanceSnapshot] = []
        for campaign_id in sorted(totals):
            bucket = totals[campaign_id]
            delivery = reconcile_delivery(
                impressions=int(bucket["impressions"]),
                clicks=int(bucket["clicks"]),
                conversions=int(bucket["conversions"]),
                cost=bucket["cost"],
                revenue=bucket["revenue"],
            )
            if delivery.repaired:
                # Several feeds may assert different columns of one slot, so an
                # inverted funnel can reach storage without any single record
                # having been invalid. The repair is right, but it is not free
                # information: it means two sources disagree about the same day.
                logger.warning(
                    "funnel_reconciled",
                    reader="metric_repository",
                    campaign_id=campaign_id,
                    clicks=int(bucket["clicks"]),
                    impressions=delivery.impressions,
                )
            snapshots.append(
                PerformanceSnapshot(
                    campaign_id=campaign_id,
                    campaign_name=names.get(campaign_id, ""),
                    impressions=delivery.impressions,
                    clicks=delivery.clicks,
                    conversions=delivery.conversions,
                    total_cost=delivery.cost,
                    total_revenue=delivery.revenue,
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
