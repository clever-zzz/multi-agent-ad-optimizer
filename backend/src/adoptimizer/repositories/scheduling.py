"""Persistence for the scheduled pull: watermarks and single-flight leases.

Two rules shape this module.

**The watermark only ever widens.** It records the span a feed has covered, not
the window of the most recent pull, so a narrow daily run does not erase the
memory of a wide backfill. Without that, the "is this run redundant" question
would answer *no* forever after any backfill, and the schedule would re-pull
history on every tick.

**The lease is claimed with one statement, not read then written.** A
read-then-write claim is a race with a window as wide as the pull itself, and the
loser of that race does not fail - it silently runs a second copy. A conditional
``UPDATE`` whose ``WHERE`` includes the expiry is atomic on both PostgreSQL and
SQLite, and the rowcount is the verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..core.clock import utcnow
from ..core.logging import get_logger
from ..infra.db.models import IngestWatermark, SchedulerLease
from .base import BaseRepository

logger = get_logger(__name__)


class WatermarkRepository(BaseRepository[IngestWatermark]):
    model = IngestWatermark
    resource_name = "ingest_watermark"

    async def for_source(self, source: str) -> IngestWatermark | None:
        """The covered span for one feed, or None if it has never been pulled."""
        return await self.session.get(IngestWatermark, source)

    async def for_sources(self, sources: Sequence[str]) -> dict[str, IngestWatermark]:
        """Watermarks for a known set of feeds, keyed by name."""
        if not sources:
            return {}
        statement = select(IngestWatermark).where(IngestWatermark.source.in_(sorted(set(sources))))
        rows = (await self.session.execute(statement)).scalars().all()
        return {row.source: row for row in rows}

    async def advance(
        self,
        *,
        source: str,
        start: date,
        end: date,
        batch_id: str | None,
        received: int,
        dry_run: bool,
    ) -> IngestWatermark | None:
        """Record that a window has been covered, widening the stored span.

        Returns None for a dry run and changes nothing. The scheduler already
        knows not to ask, and the guard lives here anyway because the failure
        mode is invisible: a rehearsal that moved the watermark would make the
        real run skip the very window the rehearsal only pretended to cover, and
        nobody notices until the data is a week old.

        ``batch_id`` and ``received`` describe the most recent contributing pull
        rather than the whole span. That asymmetry is deliberate - the span
        answers "what is covered", the batch id answers "where do I look next",
        and the second question is always about the newest row.
        """
        if dry_run:
            return None

        row = await self.for_source(source)
        if row is None:
            return await self.add(
                IngestWatermark(
                    source=source,
                    window_start=start,
                    window_end=end,
                    batch_id=batch_id,
                    received=received,
                    dry_run=False,
                )
            )

        row.window_start = min(row.window_start, start)
        row.window_end = max(row.window_end, end)
        row.batch_id = batch_id
        row.received = received
        row.dry_run = False
        await self.flush()
        return row


class LeaseRepository(BaseRepository[SchedulerLease]):
    model = SchedulerLease
    resource_name = "scheduler_lease"

    async def by_name(self, name: str) -> SchedulerLease | None:
        return await self.session.get(SchedulerLease, name)

    async def by_names(self, names: Sequence[str]) -> dict[str, SchedulerLease]:
        """Leases for a known set of jobs, keyed by name."""
        if not names:
            return {}
        statement = select(SchedulerLease).where(SchedulerLease.name.in_(sorted(set(names))))
        rows = (await self.session.execute(statement)).scalars().all()
        return {row.name: row for row in rows}

    async def acquire(
        self,
        name: str,
        *,
        holder: str,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> tuple[bool, SchedulerLease | None]:
        """Claim the lease if it is free. Returns whether we won, and the row.

        The conditional UPDATE is the whole protocol. Two callers can both issue
        it and exactly one sees ``rowcount == 1``, because PostgreSQL locks the
        row for the statement and SQLite serialises the write.

        Inserting the row on first use is the only racy part, and the primary key
        arbitrates it: the loser of that race rolls back and re-reads, which now
        finds a row somebody else holds. The rollback discards the whole session,
        so a caller must acquire the lease in a unit of work of its own rather
        than alongside work it cannot afford to lose.
        """
        moment = now or utcnow()
        statement = (
            update(SchedulerLease)
            .where(
                SchedulerLease.name == name,
                or_(
                    SchedulerLease.expires_at.is_(None),
                    SchedulerLease.expires_at < moment,
                ),
            )
            .values(holder=holder, acquired_at=moment, expires_at=moment + ttl)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        if int(result.rowcount or 0) == 1:
            await self.flush()
            logger.debug("scheduler_lease_acquired", lease=name, holder=holder)
            return True, await self.by_name(name)

        existing = await self.by_name(name)
        if existing is not None:
            return False, existing

        try:
            row = await self.add(
                SchedulerLease(
                    name=name,
                    holder=holder,
                    acquired_at=moment,
                    expires_at=moment + ttl,
                )
            )
        except IntegrityError:
            await self.session.rollback()
            logger.debug("scheduler_lease_insert_race_lost", lease=name, holder=holder)
            return False, await self.by_name(name)
        return True, row

    async def record_outcome(
        self,
        name: str,
        *,
        holder: str,
        outcome: str,
        message: str = "",
        batch_id: str | None = None,
        failed: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """Let the lease go and record how the holder got on.

        Guarded on ``holder`` so that a pull which outlived its TTL cannot
        release the lease of whoever took over - that would let two holders
        alternately free each other and defeat the point of the lock. Returning
        False therefore means "you no longer held it", which is worth a log line
        and a longer TTL, not a retry.

        The row is cleared rather than deleted: "who ran this last and what
        happened" is the first question when a feed goes quiet, and a deleted row
        cannot answer it.
        """
        failures: Any = SchedulerLease.consecutive_failures + 1 if failed else 0
        statement = (
            update(SchedulerLease)
            .where(SchedulerLease.name == name, SchedulerLease.holder == holder)
            .values(
                holder="",
                expires_at=None,
                last_outcome=outcome,
                last_message=message[:300],
                last_batch_id=batch_id,
                last_finished_at=now or utcnow(),
                consecutive_failures=failures,
            )
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        await self.flush()
        released = int(result.rowcount or 0) == 1
        if not released:
            logger.warning(
                "scheduler_lease_release_lost",
                lease=name,
                holder=holder,
                outcome=outcome,
            )
        return released
