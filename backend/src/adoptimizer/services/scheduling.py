"""The scheduled pull.

Three questions decide whether a scheduler is trustworthy, and this module
answers each of them out loud instead of leaving them implicit in a cron
expression:

1. **What window does this run cover?** Derived from the calendar and the
   configured lookback, never from a stored cursor. A schedule driven by "where I
   left off" inherits every mistake in that record forever, and silently: each
   run is correct relative to the last, so a gap never surfaces as an error, only
   as data that is missing.
2. **What happens when a run is missed?** The next window reaches back to the day
   after the watermark ended, bounded by ``max_catchup_days``. Past that bound the
   plan reports ``gap_days`` rather than quietly shrinking the window, because the
   fix is a deliberate backfill and somebody has to be told one is owed.
3. **What happens when two runs overlap?** A lease in the database, claimed with
   one conditional statement. ``concurrencyPolicy: Forbid`` arbitrates between
   jobs from a single CronJob and nothing else; two replicas of the resident loop,
   or a loop overlapping a manual trigger, need a lock they can all see.

The tick is idempotent end to end. Re-pulling a window re-asserts the same
numbers through the ingestion upsert, and an absent column never overwrites a
stored one, so a lost lease, a restarted pod and a manual trigger are all safe.
That is what allows the schedule to be approximate: correctness depends on it
firing eventually, not exactly once.

Transactions are split three ways per tick, each committed on its own:

- acquire the lease, so other holders see the claim before the pull starts;
- pull, ingest and advance the watermark as one unit, so a window is never marked
  covered by rows that rolled back;
- record the outcome and let the lease go, which has to survive a failed pull.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.clock import as_utc, utc_today, utcnow
from ..core.config import IngestSettings
from ..core.logging import get_logger
from ..core.metrics import INGEST_LAG_DAYS, INGEST_TICKS_TOTAL
from ..infra.db.models import IngestWatermark, SchedulerLease
from ..infra.ingest import MetricSourceRegistry
from ..repositories.scheduling import LeaseRepository, WatermarkRepository
from ..schemas.ingest import IngestReportOut
from ..schemas.scheduling import (
    LeaseOut,
    PlanReason,
    PullPlanOut,
    ScheduledSourceOut,
    SchedulerStatusOut,
    TickOut,
    TickReportOut,
    TickResult,
    WatermarkOut,
)
from .ingest import IngestService

logger = get_logger(__name__)


def default_holder() -> str:
    """Identify this process to the others competing for the same lease."""
    return socket.gethostname() + ":" + str(os.getpid())


def plan_window(
    source: str,
    *,
    today: date,
    lookback_days: int,
    max_catchup_days: int,
    covered: tuple[date, date] | None = None,
) -> PullPlanOut:
    """Decide what the next pull for one feed should ask for.

    Pure on purpose: this is the missed-run policy, it is the part worth arguing
    about, and it should be testable without a database, a clock or a container.

    ``covered`` is the stored ``(window_start, window_end)`` span, or None when
    the feed has never been pulled. Windows are inclusive at both ends.
    """
    end = today
    nominal_start = today - timedelta(days=lookback_days - 1)
    floor = today - timedelta(days=max_catchup_days - 1)

    def build(
        start: date,
        reason: PlanReason,
        detail: str,
        *,
        catchup_days: int = 0,
        gap_days: int = 0,
        is_covered: bool = False,
    ) -> PullPlanOut:
        return PullPlanOut(
            source=source,
            start=start,
            end=end,
            days=(end - start).days + 1,
            reason=reason,
            detail=detail,
            catchup_days=catchup_days,
            gap_days=gap_days,
            covered=is_covered,
        )

    if covered is None:
        return build(
            nominal_start,
            PlanReason.FIRST,
            "no watermark for this feed, so this pulls the nominal lookback. "
            "History before it is a deliberate backfill, not something a "
            "scheduler should invent on its own initiative.",
        )

    covered_start, covered_end = covered
    if covered_start <= nominal_start and covered_end >= end:
        return build(
            nominal_start,
            PlanReason.COVERED,
            "already covered "
            + covered_start.isoformat()
            + ".."
            + covered_end.isoformat()
            + "; pulling again would only re-assert what is stored",
            is_covered=True,
        )

    wanted = covered_end + timedelta(days=1)
    if wanted >= nominal_start:
        return build(
            nominal_start,
            PlanReason.NOMINAL,
            "watermark ends "
            + covered_end.isoformat()
            + "; the nominal lookback overlaps it, so the overlap re-checks the "
            "days a platform may still be revising",
        )

    if wanted >= floor:
        return build(
            wanted,
            PlanReason.CATCHUP,
            "watermark ends " + covered_end.isoformat() + "; reaching back over the missed run(s)",
            catchup_days=(nominal_start - wanted).days,
        )

    return build(
        floor,
        PlanReason.CAPPED,
        "the gap starts "
        + wanted.isoformat()
        + " but INGEST__MAX_CATCHUP_DAYS stops the window at "
        + floor.isoformat()
        + "; run adoptimizer ingest --start "
        + wanted.isoformat()
        + " --end "
        + (floor - timedelta(days=1)).isoformat()
        + " to backfill the rest",
        catchup_days=(nominal_start - floor).days,
        gap_days=(floor - wanted).days,
    )


def _watermark_out(row: IngestWatermark, *, today: date) -> WatermarkOut:
    return WatermarkOut(
        source=row.source,
        window_start=row.window_start,
        window_end=row.window_end,
        batch_id=row.batch_id,
        received=row.received,
        dry_run=row.dry_run,
        updated_at=as_utc(row.updated_at) if row.updated_at is not None else None,
        lag_days=(today - row.window_end).days,
    )


def _lease_out(row: SchedulerLease, *, now: datetime) -> LeaseOut:
    expires = as_utc(row.expires_at) if row.expires_at is not None else None
    return LeaseOut(
        name=row.name,
        holder=row.holder,
        held=expires is not None and expires > now,
        acquired_at=as_utc(row.acquired_at) if row.acquired_at is not None else None,
        expires_at=expires,
        last_outcome=row.last_outcome,
        last_message=row.last_message,
        last_batch_id=row.last_batch_id,
        last_finished_at=as_utc(row.last_finished_at) if row.last_finished_at is not None else None,
        consecutive_failures=row.consecutive_failures,
    )


class IngestScheduler:
    """Pulls the configured feeds on a cadence, one at a time, without gaps."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sources: MetricSourceRegistry,
        settings: IngestSettings,
        *,
        holder: str = "",
    ) -> None:
        self._sessions = session_factory
        self._sources = sources
        self._settings = settings
        self._holder = holder or default_holder()

    @property
    def holder(self) -> str:
        return self._holder

    @property
    def settings(self) -> IngestSettings:
        return self._settings

    def lease_name(self, source: str) -> str:
        """One lease per feed, so a slow platform cannot block the others."""
        return "ingest:" + source

    @asynccontextmanager
    async def _unit(self) -> AsyncIterator[AsyncSession]:
        session = self._sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def _plan_with_watermark(
        self, source: str, *, today: date | None = None
    ) -> tuple[PullPlanOut, IngestWatermark | None]:
        day = today or utc_today()
        async with self._unit() as session:
            row = await WatermarkRepository(session).for_source(source)
            span = (row.window_start, row.window_end) if row is not None else None
        plan = plan_window(
            source,
            today=day,
            lookback_days=self._settings.lookback_days,
            max_catchup_days=self._settings.max_catchup_days,
            covered=span,
        )
        return plan, row

    async def tick(
        self,
        source: str,
        *,
        today: date | None = None,
        dry_run: bool | None = None,
        force: bool = False,
    ) -> TickOut:
        """One scheduled attempt at one feed.

        Never raises. A tick that fails is reported as a failed tick, because
        propagating would turn one uncredentialed platform into a pass that never
        reaches the feeds queued behind it. Cancellation is a BaseException and
        still unwinds, which is what lets a shutdown stop the loop.
        """
        started = time.perf_counter()
        rehearsal = self._settings.dry_run if dry_run is None else dry_run
        lease = self.lease_name(source)

        plan, watermark = await self._plan_with_watermark(source, today=today)
        if watermark is not None:
            # Set only when there is something to measure against. A feed that has
            # never been pulled has no lag yet, and inventing one would erase the
            # difference between "new feed" and "stale feed" - a difference
            # absent() already reports correctly.
            INGEST_LAG_DAYS.labels(source=source).set((plan.end - watermark.window_end).days)

        if plan.covered and not force:
            return self._tick_out(source, TickResult.SKIPPED, plan, started)

        won, row = await self._acquire(lease)
        if not won:
            holder = row.holder if row is not None else ""
            return self._tick_out(
                source,
                TickResult.LOST_LEASE,
                plan,
                started,
                detail="held by " + (holder or "an unidentified process"),
            )

        try:
            report = await self._pull(source, plan, rehearsal)
        except Exception as exc:
            message = (type(exc).__name__ + ": " + str(exc))[:300]
            await self._release(lease, TickResult.FAILED, message=message, failed=True)
            logger.error(
                "ingest_tick_failed",
                source=source,
                window_start=plan.start.isoformat(),
                window_end=plan.end.isoformat(),
                reason=plan.reason.value,
                error=str(exc),
            )
            return self._tick_out(source, TickResult.FAILED, plan, started, error=message)

        await self._release(lease, TickResult.RAN, message=plan.detail, batch_id=report.batch_id)
        outcome = self._tick_out(source, TickResult.RAN, plan, started, report=report)
        logger.info(
            "ingest_tick_complete",
            source=source,
            window_start=plan.start.isoformat(),
            window_end=plan.end.isoformat(),
            reason=plan.reason.value,
            gap_days=plan.gap_days,
            dry_run=rehearsal,
            received=report.received,
            created=report.created,
            updated=report.updated,
            rejected=report.rejected_count,
            unresolved=report.unresolved_count,
            batch_id=report.batch_id,
            duration_ms=outcome.duration_ms,
        )
        return outcome

    async def tick_all(
        self,
        *,
        today: date | None = None,
        dry_run: bool | None = None,
        force: bool = False,
    ) -> TickReportOut:
        """One pass over every configured feed, in configured order."""
        ticks = [
            await self.tick(source, today=today, dry_run=dry_run, force=force)
            for source in self._settings.sources
        ]
        return TickReportOut(
            ticks=ticks,
            ran=sum(1 for tick in ticks if tick.outcome is TickResult.RAN),
            skipped=sum(1 for tick in ticks if tick.outcome is TickResult.SKIPPED),
            lost_lease=sum(1 for tick in ticks if tick.outcome is TickResult.LOST_LEASE),
            failed=sum(1 for tick in ticks if tick.outcome is TickResult.FAILED),
        )

    async def status(self, *, today: date | None = None) -> SchedulerStatusOut:
        """The whole schedule in one read, for the diagnostics endpoint."""
        day = today or utc_today()
        names = list(self._settings.sources)
        lease_names = [self.lease_name(name) for name in names]
        async with self._unit() as session:
            marks = await WatermarkRepository(session).for_sources(names)
            leases = await LeaseRepository(session).by_names(lease_names)

        known = self._sources.status()
        moment = utcnow()
        entries: list[ScheduledSourceOut] = []
        for source in names:
            row = marks.get(source)
            span = (row.window_start, row.window_end) if row is not None else None
            lease_row = leases.get(self.lease_name(source))
            entries.append(
                ScheduledSourceOut(
                    source=source,
                    registered=source in self._sources,
                    configured=bool(known.get(source, {}).get("configured")),
                    plan=plan_window(
                        source,
                        today=day,
                        lookback_days=self._settings.lookback_days,
                        max_catchup_days=self._settings.max_catchup_days,
                        covered=span,
                    ),
                    watermark=_watermark_out(row, today=day) if row is not None else None,
                    lease=_lease_out(lease_row, now=moment) if lease_row is not None else None,
                )
            )

        return SchedulerStatusOut(
            enabled=self._settings.scheduler_enabled,
            interval_minutes=self._settings.interval_minutes,
            lookback_days=self._settings.lookback_days,
            max_catchup_days=self._settings.max_catchup_days,
            dry_run=self._settings.dry_run,
            lease_ttl_seconds=self._settings.lease_ttl_seconds,
            today=day,
            sources=entries,
        )

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Tick until asked to stop.

        The interval doubles as the retry backoff and deliberately does not
        change on failure. What usually fails is a platform rate limit or a
        database blip, and neither is helped by a tighter loop; a backoff that
        shrinks under error is how an outage becomes a denial of service against
        the thing that is already down. A pass that outlives the interval simply
        runs back to back, which the lease makes safe.
        """
        signal = stop if stop is not None else asyncio.Event()
        seconds = self._settings.interval_minutes * 60
        logger.info(
            "ingest_scheduler_started",
            holder=self._holder,
            sources=list(self._settings.sources),
            interval_minutes=self._settings.interval_minutes,
            lookback_days=self._settings.lookback_days,
            max_catchup_days=self._settings.max_catchup_days,
            dry_run=self._settings.dry_run,
        )
        while not signal.is_set():
            report = await self.tick_all()
            if not report.ok:
                logger.error(
                    "ingest_scheduler_pass_failed",
                    failed=report.failed,
                    sources=[tick.source for tick in report.ticks if tick.error is not None],
                )
            try:
                # wait_for rather than sleep so a shutdown lands immediately
                # instead of at the end of a six-hour interval. A process that
                # takes hours to notice SIGTERM is a process that gets SIGKILLed.
                await asyncio.wait_for(signal.wait(), timeout=seconds)
            except TimeoutError:
                continue
        logger.info("ingest_scheduler_stopped", holder=self._holder)

    async def _acquire(self, lease: str) -> tuple[bool, SchedulerLease | None]:
        async with self._unit() as session:
            return await LeaseRepository(session).acquire(
                lease,
                holder=self._holder,
                ttl=timedelta(seconds=self._settings.lease_ttl_seconds),
            )

    async def _release(
        self,
        lease: str,
        outcome: TickResult,
        *,
        message: str = "",
        batch_id: str | None = None,
        failed: bool = False,
    ) -> None:
        try:
            async with self._unit() as session:
                await LeaseRepository(session).record_outcome(
                    lease,
                    holder=self._holder,
                    outcome=outcome.value,
                    message=message,
                    batch_id=batch_id,
                    failed=failed,
                )
        except Exception as exc:
            # Losing the record is not losing the pull. The lease expires on its
            # own, so the schedule recovers without this write, and raising here
            # would report a tick as failed after its data had already landed.
            logger.warning(
                "ingest_tick_outcome_unrecorded",
                lease=lease,
                outcome=outcome.value,
                error=str(exc),
            )

    async def _pull(self, source: str, plan: PullPlanOut, rehearsal: bool) -> IngestReportOut:
        async with self._unit() as session:
            report = await IngestService(session).pull(
                self._sources,
                source,
                start=plan.start,
                end=plan.end,
                dry_run=rehearsal,
                actor="scheduler:" + self._holder,
            )
            # Same transaction as the rows it describes: a window must never be
            # marked covered by an ingest that rolled back, or the gap becomes
            # both permanent and invisible.
            await WatermarkRepository(session).advance(
                source=source,
                start=plan.start,
                end=plan.end,
                batch_id=report.batch_id,
                received=report.received,
                dry_run=rehearsal,
            )
        return report

    def _tick_out(
        self,
        source: str,
        outcome: TickResult,
        plan: PullPlanOut,
        started: float,
        *,
        detail: str = "",
        report: IngestReportOut | None = None,
        error: str | None = None,
    ) -> TickOut:
        # The single exit for every tick, which is what makes it the right place
        # to count: a skipped window and a lost lease are both healthy outcomes
        # that an alert needs to be able to tell apart from a failure.
        INGEST_TICKS_TOTAL.labels(source=source, outcome=outcome.value).inc()
        return TickOut(
            source=source,
            outcome=outcome,
            reason=plan.reason,
            detail=detail or plan.detail,
            start=plan.start,
            end=plan.end,
            gap_days=plan.gap_days,
            batch_id=report.batch_id if report is not None else None,
            received=report.received if report is not None else 0,
            created=report.created if report is not None else 0,
            updated=report.updated if report is not None else 0,
            rejected_count=report.rejected_count if report is not None else 0,
            unresolved_count=report.unresolved_count if report is not None else 0,
            error=error,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


__all__ = ["IngestScheduler", "default_holder", "plan_window"]
