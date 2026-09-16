"""Mirroring the primary datastore's metrics into the analytical warehouse.

Why a mirror rather than a dual write: the two stores have different jobs. The
primary datastore is transactional and authoritative - an ingestion batch is one
unit of work there, and a failed write rolls back. The warehouse is an
analytical replica that is eventually consistent by design.

Writing to both inside one transaction would mean a warehouse outage either
blocks ingestion or forces the batch to roll back. That is the wrong trade for
data the warehouse can always re-derive from the primary, and it would put an
analytics dependency on the critical path of taking in data. So the mirror runs
separately and is idempotent instead: the aggregate table is a
``ReplacingMergeTree`` keyed on the same tuple the source uses as its uniqueness
constraint, so replaying a window corrects rows rather than duplicating them.

That idempotence is what makes a backfill safe to re-run, and re-running is what
an operator does after a partial failure - so it is a load-bearing property, not
a nicety.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..core.metrics import WAREHOUSE_WRITES_TOTAL
from ..infra.analytics import AnalyticsSink, WriteReport
from ..infra.analytics.sink import ClickHouseSink
from ..repositories.campaigns import MetricRepository

logger = get_logger(__name__)

DEFAULT_SYNC_DAYS = 30

# The table this service targets. Named here rather than on the sink instance so
# a failure can be counted even when the sink itself is the thing that broke.
TARGET_TABLE = ClickHouseSink.DAILY_TABLE


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What a mirror run did.

    ``candidates`` and ``report.written`` are kept apart on purpose. A run that
    read 4,000 rows and wrote none is a failure that a single "rows" number
    would hide, and that is precisely the failure mode a misconfigured warehouse
    produces.
    """

    days: int
    dry_run: bool
    candidates: int
    report: WriteReport

    @property
    def written(self) -> int:
        return self.report.written

    @property
    def ok(self) -> bool:
        """A run only succeeds if the rows it found actually landed."""
        if self.dry_run or self.candidates == 0:
            return True
        return self.written > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "ok": self.ok,
            "warehouse": self.report.to_dict(),
        }


class WarehouseSyncService:
    """Copies daily metrics from the primary datastore into the warehouse."""

    def __init__(self, session: AsyncSession, sink: AnalyticsSink) -> None:
        self._session = session
        self._sink = sink
        self._metrics = MetricRepository(session)

    async def status(self) -> dict[str, Any]:
        """Whether the configured sink can accept writes at all."""
        return {
            "sink": self._sink.name,
            "available": self._sink.is_available,
            "health": await self._sink.healthcheck(),
        }

    async def sync(
        self,
        *,
        days: int = DEFAULT_SYNC_DAYS,
        campaign_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> SyncReport:
        """Mirror the last ``days`` of daily metrics.

        ``dry_run`` reads the rows and reports the count without writing, which
        is how an operator answers "how much would this move" before pointing a
        backfill at a production warehouse.
        """
        rows = await self._metrics.export_daily(days=days, campaign_ids=campaign_ids)
        logger.info("warehouse_sync_started", candidates=len(rows), days=days, dry_run=dry_run)

        if dry_run:
            return SyncReport(
                days=days,
                dry_run=True,
                candidates=len(rows),
                report=WriteReport(
                    table=TARGET_TABLE,
                    skipped=len(rows),
                    reasons={"dry_run": len(rows)} if rows else {},
                ),
            )

        if not rows:
            # Nothing to move is a success, not a failure: a fresh install has
            # no metrics yet and a backfill of a quiet window is legitimate.
            return SyncReport(
                days=days,
                dry_run=False,
                candidates=0,
                report=WriteReport(table=TARGET_TABLE),
            )

        try:
            report = await self._sink.write_daily(rows)
        except Exception:
            WAREHOUSE_WRITES_TOTAL.labels(table=TARGET_TABLE, outcome="failed").inc(len(rows))
            logger.error("warehouse_sync_failed", candidates=len(rows), days=days)
            raise

        if report.written:
            WAREHOUSE_WRITES_TOTAL.labels(table=TARGET_TABLE, outcome="written").inc(report.written)
        if report.skipped:
            WAREHOUSE_WRITES_TOTAL.labels(table=TARGET_TABLE, outcome="skipped").inc(report.skipped)

        logger.info("warehouse_sync_finished", **report.to_dict())
        return SyncReport(days=days, dry_run=False, candidates=len(rows), report=report)
