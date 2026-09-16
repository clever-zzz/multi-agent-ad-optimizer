"""The write side of the analytical warehouse.

The read side (``infra.warehouse``) degrades to an empty result when ClickHouse
is unreachable, because a warehouse outage must not take the optimization loop
down with it. Writes take the opposite stance and raise. A read that returns
nothing leaves the optimizer running on the primary datastore, which is
recoverable; a write that quietly does nothing loses data that nothing else
holds a copy of.

Three properties are deliberate:

* **Batched.** Rows go in chunks of ``BATCH_SIZE`` so a backfill of a year of
  daily metrics does not build one enormous statement in memory.
* **Reported.** Every refused row is counted against a stable reason, so a feed
  that starts sending a device of ``"ctv"`` shows up as a number rather than as
  a mystery hole in a dashboard.
* **Idempotent at the table level.** ``campaign_daily_metrics`` is a
  ``ReplacingMergeTree`` keyed on (campaign, creative, date), so replaying a
  backfill corrects rows instead of duplicating them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from ...core.config import ClickHouseSettings, get_settings
from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from .models import AdEvent, DailyMetricRow, WriteReport, daily_row, normalise_event

logger = get_logger(__name__)

# Reported by the null sink. Distinct from the per-row reasons in ``models``
# because it describes the deployment, not the data.
REASON_SINK_DISABLED = "warehouse_not_configured"


class AnalyticsSink(Protocol):
    """Where analytical rows are written."""

    name: str

    @property
    def is_available(self) -> bool: ...

    async def write_events(self, events: Sequence[AdEvent]) -> WriteReport: ...

    async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport: ...

    async def healthcheck(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class NullSink:
    """Used when no warehouse is configured.

    It reports every row as skipped with one reason, so a caller that forgot to
    configure a warehouse sees a number instead of a false success. This is the
    whole point: the failure mode to avoid is a backfill that prints "done" over
    a warehouse that received nothing.
    """

    name = "null"
    is_available = False

    def __init__(self, reason: str = REASON_SINK_DISABLED) -> None:
        self._reason = reason

    def _report(self, table: str, count: int) -> WriteReport:
        if not count:
            return WriteReport(table=table)
        return WriteReport(table=table, skipped=count, reasons={self._reason: count})

    async def write_events(self, events: Sequence[AdEvent]) -> WriteReport:
        return self._report(ClickHouseSink.EVENTS_TABLE, len(events))

    async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport:
        return self._report(ClickHouseSink.DAILY_TABLE, len(rows))

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "status": "disabled", "reason": self._reason}

    async def close(self) -> None:
        return None


class ClickHouseSink:
    """Bulk-writes analytics rows into ClickHouse."""

    name = "clickhouse"

    EVENTS_TABLE = "ad_events"
    DAILY_TABLE = "campaign_daily_metrics"

    # Columns are declared explicitly rather than inferred from the first row:
    # a batch whose first row is missing a key would otherwise define the shape
    # for every row after it.
    EVENT_COLUMNS: tuple[str, ...] = (
        "event_id",
        "campaign_id",
        "creative_id",
        "event_type",
        "cost",
        "revenue",
        "platform",
        "device",
        "country",
        "age_group",
        "gender",
        "event_time",
    )
    DAILY_COLUMNS: tuple[str, ...] = (
        "campaign_id",
        "stat_date",
        "creative_id",
        "impressions",
        "clicks",
        "conversions",
        "cost",
        "revenue",
        "unique_reach",
        "source",
    )

    BATCH_SIZE = 1000

    def __init__(self, settings: ClickHouseSettings, *, client: Any = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def is_available(self) -> bool:
        return self._client is not None

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
            logger.info("clickhouse_sink_connected", database=self._settings.database)
            return True
        except Exception as exc:
            logger.warning("clickhouse_sink_unavailable", error=str(exc))
            self._client = None
            return False

    async def write_events(self, events: Sequence[AdEvent]) -> WriteReport:
        """Normalise and insert events, reporting every row that was refused."""
        rows: list[tuple[Any, ...]] = []
        reasons: dict[str, int] = {}
        for event in events:
            row, reason = normalise_event(event)
            if row is None:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            rows.append(tuple(row[column] for column in self.EVENT_COLUMNS))

        written = await self._insert(self.EVENTS_TABLE, rows, self.EVENT_COLUMNS)
        if reasons:
            logger.warning(
                "clickhouse_events_skipped", skipped=len(events) - written, reasons=reasons
            )
        return WriteReport(
            table=self.EVENTS_TABLE,
            written=written,
            skipped=len(events) - written,
            reasons=reasons,
        )

    async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport:
        """Insert daily aggregates. Nothing is refused; see ``daily_row``."""
        prepared = [
            tuple(daily_row(metric)[column] for column in self.DAILY_COLUMNS) for metric in rows
        ]
        written = await self._insert(self.DAILY_TABLE, prepared, self.DAILY_COLUMNS)
        return WriteReport(table=self.DAILY_TABLE, written=written)

    async def _insert(
        self, table: str, rows: list[tuple[Any, ...]], columns: tuple[str, ...]
    ) -> int:
        """Insert in batches, off the event loop, failing loudly."""
        if self._client is None:
            raise ExternalServiceError("ClickHouse sink is not connected; cannot write to " + table)
        if not rows:
            return 0

        for start in range(0, len(rows), self.BATCH_SIZE):
            batch = rows[start : start + self.BATCH_SIZE]
            await _to_thread(self._client.insert, table, batch, column_names=list(columns))
        logger.info("clickhouse_rows_written", table=table, rows=len(rows))
        return len(rows)

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
                logger.debug("clickhouse_sink_close_failed")
            self._client = None


async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking driver call off the event loop."""
    import asyncio

    return await asyncio.to_thread(lambda: func(*args, **kwargs))


async def build_sink(settings: ClickHouseSettings | None = None) -> AnalyticsSink:
    """Return a connected ClickHouse sink, or a null sink when unavailable.

    Mirrors ``build_warehouse``: an unconfigured or unreachable warehouse is not
    an error at construction time, because a deployment may legitimately run
    without one. The difference is that the null sink reports every write as
    skipped rather than pretending it landed.
    """
    resolved = settings or get_settings().clickhouse
    if not resolved.enabled:
        return NullSink("clickhouse_disabled")

    sink = ClickHouseSink(resolved)
    if await sink.connect():
        return sink

    logger.warning("clickhouse_enabled_but_unreachable_writes_will_not_land")
    return NullSink("clickhouse_unreachable")
