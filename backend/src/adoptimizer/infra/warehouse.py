"""Analytical warehouse access.

Two implementations share one protocol: ClickHouse for real event volumes and a
SQL aggregate reader for environments without a warehouse. Callers never branch
on which one is active.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Protocol

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utc_today
from ..core.config import ClickHouseSettings, get_settings
from ..core.logging import get_logger
from ..domain.audience import SegmentObservation
from ..domain.kpi import PerformanceSnapshot
from .db.models import Campaign, DailyMetric

logger = get_logger(__name__)

AUDIENCE_DIMENSIONS = ("device", "country", "age_group", "gender")

# The numeric measures both warehouse backends report for a bucket of delivery.
MEASURES = ("impressions", "clicks", "conversions", "cost", "revenue")


def _accumulate(target: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one slot's measures into a running total."""
    for measure in MEASURES:
        target[measure] = target.get(measure, 0) + row[measure]


class MetricsWarehouse(Protocol):
    """Read model for campaign telemetry."""

    name: str

    async def campaign_snapshots(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[PerformanceSnapshot]: ...

    async def creative_snapshots(self, campaign_id: str, *, days: int) -> list[dict[str, Any]]: ...

    async def audience_observations(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[SegmentObservation]: ...

    async def timeseries(self, campaign_id: str | None, *, days: int) -> list[dict[str, Any]]: ...

    async def healthcheck(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class SqlAggregateWarehouse:
    """Reads the daily aggregate table from the primary database.

    ``daily_metrics`` stores two granularities side by side: a campaign-level
    slot with ``creative_id IS NULL`` holding the day's total, and one row per
    creative holding the breakdown of that same total. The partial unique index
    on the model exists to protect exactly that split. Summing both granularities
    would double every KPI, so campaign and timeseries aggregates read the
    campaign-level slot, and fall back to the breakdown only for buckets that
    have no campaign-level slot at all.
    """

    name = "sql"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _slot(breakdown: bool) -> Any:
        """Predicate selecting exactly one of the two stored granularities."""
        if breakdown:
            return DailyMetric.creative_id.is_not(None)
        return DailyMetric.creative_id.is_(None)

    @staticmethod
    def _measures(row: Any) -> dict[str, Any]:
        """Normalise one aggregate row into plain numeric measures."""
        return {
            "impressions": int(row.impressions or 0),
            "clicks": int(row.clicks or 0),
            "conversions": int(row.conversions or 0),
            "cost": float(row.cost or 0.0),
            "revenue": float(row.revenue or 0.0),
        }

    async def _slots(
        self,
        cutoff: date,
        *,
        breakdown: bool,
        campaign_ids: list[str] | None = None,
        campaign_id: str | None = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Delivery per (campaign, day) from exactly one of the two granularities."""
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
            .where(DailyMetric.stat_date >= cutoff, self._slot(breakdown))
            .group_by(DailyMetric.campaign_id, DailyMetric.stat_date, Campaign.name)
        )
        if campaign_ids:
            statement = statement.where(DailyMetric.campaign_id.in_(campaign_ids))
        if campaign_id:
            statement = statement.where(DailyMetric.campaign_id == campaign_id)

        rows = (await self._session.execute(statement)).all()
        return {
            (str(row.campaign_id), row.stat_date.isoformat()): self._measures(row)
            | {"name": str(row.campaign_name or "")}
            for row in rows
        }

    async def _merged_slots(
        self,
        cutoff: date,
        *,
        campaign_ids: list[str] | None = None,
        campaign_id: str | None = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Both granularities merged slot by slot, the campaign-level one winning.

        Merging at (campaign, day) rather than per campaign is what makes a mixed
        portfolio exact: a campaign that stores its roll-up contributes that, and
        one ingested only per creative still contributes its delivery instead of
        reading as zero and disappearing from optimization.
        """
        slots = await self._slots(
            cutoff, breakdown=False, campaign_ids=campaign_ids, campaign_id=campaign_id
        )
        breakdown = await self._slots(
            cutoff, breakdown=True, campaign_ids=campaign_ids, campaign_id=campaign_id
        )
        for key, row in breakdown.items():
            slots.setdefault(key, row)
        return slots

    async def campaign_snapshots(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[PerformanceSnapshot]:
        cutoff = utc_today() - timedelta(days=days - 1)
        slots = await self._merged_slots(cutoff, campaign_ids=campaign_ids)

        totals: dict[str, dict[str, Any]] = {}
        names: dict[str, str] = {}
        for (bucket, _day), row in slots.items():
            _accumulate(totals.setdefault(bucket, {}), row)
            names.setdefault(bucket, str(row.get("name", "")))

        return [
            PerformanceSnapshot(
                campaign_id=bucket,
                campaign_name=names[bucket],
                impressions=int(totals[bucket]["impressions"]),
                clicks=int(totals[bucket]["clicks"]),
                conversions=int(totals[bucket]["conversions"]),
                total_cost=float(totals[bucket]["cost"]),
                total_revenue=float(totals[bucket]["revenue"]),
            )
            for bucket in sorted(totals)
        ]

    async def creative_snapshots(self, campaign_id: str, *, days: int) -> list[dict[str, Any]]:
        cutoff = utc_today() - timedelta(days=days - 1)
        statement = (
            select(
                DailyMetric.creative_id,
                func.sum(DailyMetric.impressions).label("impressions"),
                func.sum(DailyMetric.clicks).label("clicks"),
                func.sum(DailyMetric.conversions).label("conversions"),
                func.sum(DailyMetric.cost).label("cost"),
                func.sum(DailyMetric.revenue).label("revenue"),
            )
            .where(
                DailyMetric.campaign_id == campaign_id,
                DailyMetric.stat_date >= cutoff,
                DailyMetric.creative_id.is_not(None),
            )
            .group_by(DailyMetric.creative_id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            {
                "creative_id": row.creative_id,
                "impressions": int(row.impressions or 0),
                "clicks": int(row.clicks or 0),
                "conversions": int(row.conversions or 0),
                "cost": float(row.cost or 0.0),
                "revenue": float(row.revenue or 0.0),
            }
            for row in rows
            if row.creative_id
        ]

    async def audience_observations(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[SegmentObservation]:
        """Daily aggregates carry no demographics, so report the campaign split."""
        snapshots = await self.campaign_snapshots(campaign_ids, days=days)
        return [
            SegmentObservation(
                key=snapshot.campaign_name or snapshot.campaign_id,
                dimension="campaign",
                impressions=snapshot.impressions,
                clicks=snapshot.clicks,
                conversions=snapshot.conversions,
                cost=snapshot.total_cost,
                revenue=snapshot.total_revenue,
            )
            for snapshot in snapshots
        ]

    async def timeseries(self, campaign_id: str | None, *, days: int) -> list[dict[str, Any]]:
        cutoff = utc_today() - timedelta(days=days - 1)
        slots = await self._merged_slots(cutoff, campaign_id=campaign_id)

        per_day: dict[str, dict[str, Any]] = {}
        for (_campaign, day), row in slots.items():
            _accumulate(per_day.setdefault(day, {}), row)

        return [
            {
                "date": day,
                "impressions": int(per_day[day]["impressions"]),
                "clicks": int(per_day[day]["clicks"]),
                "conversions": int(per_day[day]["conversions"]),
                "cost": round(float(per_day[day]["cost"]), 2),
                "revenue": round(float(per_day[day]["revenue"]), 2),
            }
            for day in sorted(per_day)
        ]

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "status": "ok"}

    async def close(self) -> None:
        return None


class ClickHouseWarehouse:
    """Reads the analytical warehouse. Requires the clickhouse extra."""

    name = "clickhouse"

    # Demographic columns available on the events table.
    DIMENSIONS: tuple[str, ...] = ("device", "country", "age_group", "gender")

    def __init__(self, settings: ClickHouseSettings) -> None:
        self._settings = settings
        self._client: Any = None

    async def connect(self) -> bool:
        """Connect lazily and report failure without raising."""
        try:
            import clickhouse_connect

            self._client = await _to_thread(
                clickhouse_connect.get_client,
                host=self._settings.host,
                port=self._settings.http_port,
                username=self._settings.user,
                password=self._settings.password.get_secret_value(),
                database=self._settings.database,
                secure=self._settings.secure,
                verify=self._settings.verify_tls,
                connect_timeout=int(self._settings.connect_timeout_seconds),
                send_receive_timeout=int(self._settings.query_timeout_seconds),
            )
            await _to_thread(self._client.command, "SELECT 1")
            logger.info("clickhouse_connected", database=self._settings.database)
            return True
        except Exception as exc:
            logger.warning("clickhouse_unavailable", error=str(exc))
            self._client = None
            return False

    async def _query(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a parameterised read query off the event loop."""
        if self._client is None:
            return []
        try:
            result = await _to_thread(self._client.query, sql, parameters=parameters)
        except Exception as exc:
            logger.error("clickhouse_query_failed", error=str(exc), sql=sql[:200])
            return []
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def _window_filter(self, days: int) -> str:
        return "event_time >= now() - toIntervalDay({days:UInt16})"

    async def campaign_snapshots(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[PerformanceSnapshot]:
        where = "WHERE " + self._window_filter(days)
        parameters: dict[str, Any] = {"db": self._settings.database, "days": days}
        if campaign_ids:
            where += " AND campaign_id IN ({ids:Array(String)})"
            parameters["ids"] = list(campaign_ids)

        sql = (
            "SELECT campaign_id,"
            " countIf(event_type = 'impression') AS impressions,"
            " countIf(event_type = 'click') AS clicks,"
            " countIf(event_type = 'conversion') AS conversions,"
            " sumIf(cost, event_type IN ('impression', 'click')) AS cost,"
            " sumIf(revenue, event_type = 'conversion') AS revenue"
            " FROM {db:Identifier}.ad_events " + where + " GROUP BY campaign_id"
        )
        rows = await self._query(sql, parameters)
        names = await self._campaign_names([str(r["campaign_id"]) for r in rows])
        return [
            PerformanceSnapshot(
                campaign_id=str(row["campaign_id"]),
                campaign_name=names.get(str(row["campaign_id"]), ""),
                impressions=int(row.get("impressions") or 0),
                clicks=int(row.get("clicks") or 0),
                conversions=int(row.get("conversions") or 0),
                total_cost=float(row.get("cost") or 0.0),
                total_revenue=float(row.get("revenue") or 0.0),
            )
            for row in rows
        ]

    async def _campaign_names(self, campaign_ids: list[str]) -> dict[str, str]:
        if not campaign_ids:
            return {}
        rows = await self._query(
            "SELECT campaign_id, any(campaign_name) AS campaign_name"
            " FROM {db:Identifier}.campaigns"
            " WHERE campaign_id IN ({ids:Array(String)}) GROUP BY campaign_id",
            {"db": self._settings.database, "ids": campaign_ids},
        )
        return {str(r["campaign_id"]): str(r.get("campaign_name") or "") for r in rows}

    async def creative_snapshots(self, campaign_id: str, *, days: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT creative_id,"
            " countIf(event_type = 'impression') AS impressions,"
            " countIf(event_type = 'click') AS clicks,"
            " countIf(event_type = 'conversion') AS conversions,"
            " sumIf(cost, event_type IN ('impression', 'click')) AS cost,"
            " sumIf(revenue, event_type = 'conversion') AS revenue"
            " FROM {db:Identifier}.ad_events"
            " WHERE " + self._window_filter(days) + " AND campaign_id = {campaign_id:String}"
            " GROUP BY creative_id"
        )
        rows = await self._query(
            sql, {"db": self._settings.database, "days": days, "campaign_id": campaign_id}
        )
        return [
            {
                "creative_id": str(row["creative_id"]),
                "impressions": int(row.get("impressions") or 0),
                "clicks": int(row.get("clicks") or 0),
                "conversions": int(row.get("conversions") or 0),
                "cost": float(row.get("cost") or 0.0),
                "revenue": float(row.get("revenue") or 0.0),
            }
            for row in rows
        ]

    async def audience_observations(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[SegmentObservation]:
        """Aggregate delivery by every demographic dimension in one pass."""
        window = self._window_filter(days)
        campaign_filter = ""
        parameters: dict[str, Any] = {"db": self._settings.database, "days": days}
        if campaign_ids:
            campaign_filter = " AND campaign_id IN ({ids:Array(String)})"
            parameters["ids"] = list(campaign_ids)

        branches = [
            "SELECT '" + dimension + "' AS dimension,"
            " toString(" + dimension + ") AS segment_key,"
            " event_type, cost, revenue"
            " FROM {db:Identifier}.ad_events WHERE " + window + campaign_filter
            for dimension in self.DIMENSIONS
        ]
        inner = " UNION ALL ".join(branches)
        sql = (
            "SELECT dimension, segment_key,"
            " countIf(event_type = 'impression') AS impressions,"
            " countIf(event_type = 'click') AS clicks,"
            " countIf(event_type = 'conversion') AS conversions,"
            " sumIf(cost, event_type IN ('impression', 'click')) AS cost,"
            " sumIf(revenue, event_type = 'conversion') AS revenue"
            " FROM (" + inner + ") GROUP BY dimension, segment_key"
        )
        rows = await self._query(sql, parameters)
        return [
            SegmentObservation(
                key=str(row["segment_key"]),
                dimension=str(row["dimension"]),
                impressions=int(row.get("impressions") or 0),
                clicks=int(row.get("clicks") or 0),
                conversions=int(row.get("conversions") or 0),
                cost=float(row.get("cost") or 0.0),
                revenue=float(row.get("revenue") or 0.0),
            )
            for row in rows
        ]

    async def timeseries(self, campaign_id: str | None, *, days: int) -> list[dict[str, Any]]:
        where = "WHERE " + self._window_filter(days)
        parameters: dict[str, Any] = {"db": self._settings.database, "days": days}
        if campaign_id:
            where += " AND campaign_id = {campaign_id:String}"
            parameters["campaign_id"] = campaign_id

        sql = (
            "SELECT toDate(event_time) AS stat_date,"
            " countIf(event_type = 'impression') AS impressions,"
            " countIf(event_type = 'click') AS clicks,"
            " countIf(event_type = 'conversion') AS conversions,"
            " sumIf(cost, event_type IN ('impression', 'click')) AS cost,"
            " sumIf(revenue, event_type = 'conversion') AS revenue"
            " FROM {db:Identifier}.ad_events " + where + " GROUP BY stat_date ORDER BY stat_date"
        )
        rows = await self._query(sql, parameters)
        return [
            {
                "date": str(row["stat_date"]),
                "impressions": int(row.get("impressions") or 0),
                "clicks": int(row.get("clicks") or 0),
                "conversions": int(row.get("conversions") or 0),
                "cost": round(float(row.get("cost") or 0.0), 2),
                "revenue": round(float(row.get("revenue") or 0.0), 2),
            }
            for row in rows
        ]

    async def healthcheck(self) -> dict[str, Any]:
        if self._client is None:
            return {"backend": self.name, "status": "unavailable"}
        try:
            await _to_thread(self._client.command, "SELECT 1")
            return {"backend": self.name, "status": "ok"}
        except Exception as exc:
            return {"backend": self.name, "status": "error", "error": str(exc)}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await _to_thread(self._client.close)
            except Exception:  # pragma: no cover - best effort shutdown
                logger.debug("clickhouse_close_failed")
            self._client = None


async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking driver call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))


async def build_warehouse(session: AsyncSession) -> MetricsWarehouse:
    """Pick the warehouse backend based on configuration and reachability."""
    settings = get_settings().clickhouse
    if settings.enabled:
        warehouse = ClickHouseWarehouse(settings)
        if await warehouse.connect():
            return warehouse
        logger.warning("clickhouse_enabled_but_unreachable_falling_back_to_sql")
    return SqlAggregateWarehouse(session)
