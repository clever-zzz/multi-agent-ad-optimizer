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
from ..core.metrics import WAREHOUSE_READS_TOTAL
from ..domain.audience import SegmentObservation
from ..domain.kpi import PerformanceSnapshot, reconcile_delivery
from .db.models import Campaign, DailyMetric

logger = get_logger(__name__)

AUDIENCE_DIMENSIONS = ("device", "country", "age_group", "gender")

# The numeric measures both warehouse backends report for a bucket of delivery.
MEASURES = ("impressions", "clicks", "conversions", "cost", "revenue")


def _accumulate(target: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one slot's measures into a running total."""
    for measure in MEASURES:
        target[measure] = target.get(measure, 0) + row[measure]


def _reconciled_snapshot(
    reader: str,
    *,
    campaign_id: str,
    campaign_name: str,
    impressions: int,
    clicks: int,
    conversions: int,
    cost: float,
    revenue: float,
) -> PerformanceSnapshot:
    """Build a snapshot through the shared funnel repair, reporting any repair.

    Both warehouse implementations and the primary-datastore reader reconcile
    through ``domain.kpi.reconcile_delivery`` so they cannot drift apart. This
    wrapper exists so that neither implementation has to remember to also report
    it: a repair means two feeds disagreed about the same slot, and that is worth
    a log line rather than a silently corrected number.
    """
    delivery = reconcile_delivery(
        impressions=impressions,
        clicks=clicks,
        conversions=conversions,
        cost=cost,
        revenue=revenue,
    )
    if delivery.repaired:
        logger.warning(
            "funnel_reconciled",
            reader=reader,
            campaign_id=campaign_id,
            clicks=clicks,
            impressions=delivery.impressions,
        )
    return PerformanceSnapshot(
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        impressions=delivery.impressions,
        clicks=delivery.clicks,
        conversions=delivery.conversions,
        total_cost=delivery.cost,
        total_revenue=delivery.revenue,
    )


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
            _reconciled_snapshot(
                self.name,
                campaign_id=bucket,
                campaign_name=names[bucket],
                impressions=int(totals[bucket]["impressions"]),
                clicks=int(totals[bucket]["clicks"]),
                conversions=int(totals[bucket]["conversions"]),
                cost=float(totals[bucket]["cost"]),
                revenue=float(totals[bucket]["revenue"]),
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
    """Reads the analytical warehouse. Requires the clickhouse extra.

    Two sources are supported, chosen by ``CLICKHOUSE__METRICS_SOURCE``:

    * ``events`` (default) reads the raw ``ad_events`` stream. This is the
      richer shape - it carries device, country and gender, so audience
      breakdowns are real rather than a fallback.
    * ``daily`` reads ``campaign_daily_metrics``, the aggregate table the
      ingestion path can populate today. Demographics are absent there by
      construction, so ``audience_observations`` degrades to the campaign split
      and says so.

    The default stays ``events`` so that turning this on does not silently
    change what an existing deployment reads.
    """

    name = "clickhouse"

    EVENTS_TABLE = "ad_events"
    DAILY_TABLE = "campaign_daily_metrics"

    # Demographic columns available on the events table.
    DIMENSIONS: tuple[str, ...] = ("device", "country", "age_group", "gender")

    def __init__(self, settings: ClickHouseSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._source = settings.metrics_source

    @property
    def source(self) -> str:
        return self._source

    @property
    def _table(self) -> str:
        return self.DAILY_TABLE if self._source == "daily" else self.EVENTS_TABLE

    @property
    def _relation(self) -> str:
        """The qualified table reference, carrying the dedup its engine needs.

        ``campaign_daily_metrics`` is a ``ReplacingMergeTree`` keyed on
        (campaign_id, creative_id, stat_date), and replacing only happens when
        ClickHouse gets around to a background merge. Until then a read sees
        every version of a row, so summing a window adds the same delivery once
        per replay - and ``warehouse sync`` is documented as re-runnable over a
        30-day window, which makes that the normal case rather than an edge one.
        From the second run onwards impressions, spend and revenue all read
        double while ROAS, a ratio of two equally inflated numbers, still looks
        perfectly sane, so nothing downstream complains and the inflated totals
        go straight into the agents' budget and bid scoring. ``FINAL`` collapses
        to the newest version per key at read time, which is what makes the
        mirror's idempotence hold for readers and not only for the table.

        This is a different double count from the one ``_delivery_relation``
        collapses: that one is two granularities of the same money stored side by
        side, this one is two versions of one row. A query needs both handled.

        ``ad_events`` is a plain ``MergeTree`` - one row is one delivery, with no
        version to supersede - so it stays the bare table and pays no dedup.
        """
        table = "{db:Identifier}." + self._table
        return table + " FINAL" if self._source == "daily" else table

    def _measures(self) -> str:
        """The aggregate expressions, which differ by what the table stores.

        ``ad_events`` holds one row per delivery, so counting is the aggregate.
        ``campaign_daily_metrics`` already holds totals, so summing is - over the
        relation ``_delivery_relation`` returns, which has already picked one of
        the two granularities that table stores. Getting the counting wrong is not
        a subtle error - it would undercount by orders of magnitude - which is why
        the two are kept visibly separate.
        """
        if self._source == "daily":
            return (
                " sum(impressions) AS impressions,"
                " sum(clicks) AS clicks,"
                " sum(conversions) AS conversions,"
                " sum(cost) AS cost,"
                " sum(revenue) AS revenue"
            )
        return (
            " countIf(event_type = 'impression') AS impressions,"
            " countIf(event_type = 'click') AS clicks,"
            " countIf(event_type = 'conversion') AS conversions,"
            " sumIf(cost, event_type IN ('impression', 'click')) AS cost,"
            " sumIf(revenue, event_type = 'conversion') AS revenue"
        )

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
        """Execute a parameterised read query off the event loop.

        Returning an empty list when the warehouse is unreachable is deliberate:
        a warehouse outage must not take the optimization loop down with it, and
        the optimizer has a working primary datastore to fall back on. What was
        missing was any way to tell that apart from "the query genuinely found
        nothing", so every outcome is counted on ``warehouse_reads_total``. A
        steady ``degraded`` rate is the signal that reports are silently being
        served from a different source than the operator assumes.
        """
        if self._client is None:
            WAREHOUSE_READS_TOTAL.labels(backend=self.name, outcome="degraded").inc()
            return []
        try:
            result = await _to_thread(self._client.query, sql, parameters=parameters)
        except Exception as exc:
            WAREHOUSE_READS_TOTAL.labels(backend=self.name, outcome="degraded").inc()
            logger.error("clickhouse_query_failed", error=str(exc), sql=sql[:200])
            return []
        rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
        WAREHOUSE_READS_TOTAL.labels(backend=self.name, outcome="ok" if rows else "empty").inc()
        return rows

    def _window_filter(self, days: int) -> str:
        """The time predicate, which is a different column per source.

        ``stat_date`` is a calendar day, so a window of ``days`` days ending today
        starts ``days - 1`` back - the arithmetic every primary-datastore reader
        uses. Without the subtraction the warehouse answers a seven-day request
        with eight days of delivery, and disagrees with the SQL path silently.
        ``greatest`` guards it because the placeholder binds as UInt16: a ``days``
        of zero would otherwise wrap to 65535 and read the whole table rather than
        nothing. ``event_time`` is an instant, so its window is a rolling ``days``
        times 24 hours and needs no such correction.
        """
        if self._source == "daily":
            return "stat_date >= today() - toIntervalDay(greatest({days:UInt16}, 1) - 1)"
        return "event_time >= now() - toIntervalDay({days:UInt16})"

    def _date_expression(self) -> str:
        """How the bucket date is derived from each table's timestamp."""
        return "stat_date" if self._source == "daily" else "toDate(event_time)"

    def _delivery_relation(self, where: str) -> str:
        """The ``FROM`` target for a campaign-level or per-day aggregate.

        ``campaign_daily_metrics`` mirrors ``daily_metrics``, and that table keeps
        two granularities of the same money side by side: a campaign-level roll-up
        row whose ``creative_id`` is empty, plus one row per creative summing to
        that roll-up. A bare ``sum(...)`` over the window therefore counts every
        impression and every unit of spend twice - and nothing about the result
        looks broken, the numbers are merely the wrong size.

        Collapsing to one row per (campaign, day) first is the warehouse-side form
        of the contract ``MetricRepository.merged_slots`` implements in Python:
        the roll-up wins a bucket that has one, and a bucket stored only per
        creative still contributes its breakdown instead of reading as zero.
        Deciding per bucket rather than per campaign is what keeps a portfolio
        that mixes both shapes exact.

        The events source needs none of this - one row is one delivery, at one
        granularity - so it stays the bare table, byte for byte.
        """
        table = self._relation
        if self._source != "daily":
            return table + " " + where
        per_bucket = ", ".join(
            "if(countIf(creative_id = '') > 0,"
            " sumIf(" + measure + ", creative_id = ''),"
            " sumIf(" + measure + ", creative_id != '')) AS " + measure
            for measure in MEASURES
        )
        return (
            "(SELECT campaign_id, stat_date, "
            + per_bucket
            + " FROM "
            + table
            + " "
            + where
            + " GROUP BY campaign_id, stat_date)"
        )

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
            + self._measures()
            + " FROM "
            + self._delivery_relation(where)
            + " GROUP BY campaign_id"
        )
        rows = await self._query(sql, parameters)
        names = await self._campaign_names([str(r["campaign_id"]) for r in rows])
        return [
            _reconciled_snapshot(
                self.name,
                campaign_id=str(row["campaign_id"]),
                campaign_name=names.get(str(row["campaign_id"]), ""),
                impressions=int(row.get("impressions") or 0),
                clicks=int(row.get("clicks") or 0),
                conversions=int(row.get("conversions") or 0),
                cost=float(row.get("cost") or 0.0),
                revenue=float(row.get("revenue") or 0.0),
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
        where = "WHERE " + self._window_filter(days) + " AND campaign_id = {campaign_id:String}"
        if self._source == "daily":
            # The aggregate table stores a campaign's roll-up row with an empty
            # creative_id. Without this filter those totals would be reported as
            # belonging to a creative whose id is the empty string.
            where += " AND creative_id != ''"
        sql = (
            "SELECT creative_id,"
            + self._measures()
            + " FROM "
            + self._relation
            + " "
            + where
            + " GROUP BY creative_id"
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
        """Aggregate delivery by every demographic dimension in one pass.

        The aggregate table carries no demographics - a daily report from an ad
        network has no device or age breakdown in the shape this project
        ingests - so that source reports the campaign split rather than
        inventing segments it cannot actually see.
        """
        if self._source == "daily":
            return await self._campaign_split(campaign_ids, days=days)

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
            " FROM {db:Identifier}." + self.EVENTS_TABLE + " WHERE " + window + campaign_filter
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

    async def _campaign_split(
        self, campaign_ids: list[str] | None, *, days: int
    ) -> list[SegmentObservation]:
        """One bucket per campaign, the only split an aggregate table supports."""
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
        where = "WHERE " + self._window_filter(days)
        parameters: dict[str, Any] = {"db": self._settings.database, "days": days}
        if campaign_id:
            where += " AND campaign_id = {campaign_id:String}"
            parameters["campaign_id"] = campaign_id

        sql = (
            "SELECT "
            + self._date_expression()
            + " AS stat_date,"
            + self._measures()
            + " FROM "
            + self._delivery_relation(where)
            + " GROUP BY stat_date ORDER BY stat_date"
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
