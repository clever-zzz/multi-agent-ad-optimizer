"""Scheduling contracts.

Read-only shapes describing what the scheduled pull intends to do and what it
last did. There is no write contract here on purpose: a schedule is driven by
configuration and by the calendar, so the only thing a caller can do over HTTP is
ask for one attempt now, and that takes no body.

The object that carries the weight is :class:`PullPlanOut`. Somebody asking "why
did the numbers not update" needs a *reason*, not a timestamp: is the window
already covered, is the run catching up after a miss, and if it is catching up,
how many days sit beyond the configured recovery bound and therefore still owe a
manual backfill. Answering that from a ``last_run_at`` column is how schedules
get distrusted.

Derived fields (``lag_days``, ``held``) are computed by whoever builds these
objects rather than read out of the database, because both go stale the moment
they are stored: a watermark's age changes every day and a lease's held-ness
changes every second.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PlanReason(StrEnum):
    """Why the scheduler chose the window it did.

    ``covered``  the stored watermark already spans this window, so pulling
    would re-assert numbers that are already there.
    ``first``    no watermark exists for this feed. The window is the nominal
    lookback; the scheduler does not invent history nobody asked for, and a real
    backfill is an explicit ``adoptimizer ingest --start ... --end ...``.
    ``nominal``  the watermark is contiguous with, or overlaps, the lookback.
    ``catchup``  runs were missed and the window reaches back to the day after
    the watermark ended, which is still inside ``max_catchup_days``.
    ``capped``   the gap is wider than ``max_catchup_days``. The window reaches
    the bound and ``gap_days`` says what is still owed.
    """

    COVERED = "covered"
    FIRST = "first"
    NOMINAL = "nominal"
    CATCHUP = "catchup"
    CAPPED = "capped"


class TickResult(StrEnum):
    """What one scheduled attempt ended up doing.

    ``skipped`` and ``lost_lease`` are both success: the data is covered, or
    somebody else is covering it right now. Only ``failed`` should page.
    """

    RAN = "ran"
    SKIPPED = "skipped"
    LOST_LEASE = "lost_lease"
    FAILED = "failed"


class PullPlanOut(BaseModel):
    """The window the next run for one feed would pull, and why."""

    model_config = ConfigDict(extra="forbid")

    source: str
    start: date
    end: date
    days: int
    reason: PlanReason
    detail: str
    catchup_days: int = 0
    gap_days: int = 0
    covered: bool = False


class WatermarkOut(BaseModel):
    """The window one feed has actually covered."""

    model_config = ConfigDict(extra="forbid")

    source: str
    window_start: date
    window_end: date
    batch_id: str | None = None
    received: int = 0
    dry_run: bool = False
    updated_at: datetime | None = None
    lag_days: int | None = None


class LeaseOut(BaseModel):
    """Who holds the single-flight guard, and how the last holder got on."""

    model_config = ConfigDict(extra="forbid")

    name: str
    holder: str = ""
    held: bool = False
    acquired_at: datetime | None = None
    expires_at: datetime | None = None
    last_outcome: str = ""
    last_message: str = ""
    last_batch_id: str | None = None
    last_finished_at: datetime | None = None
    consecutive_failures: int = 0


class ScheduledSourceOut(BaseModel):
    """One configured feed with everything needed to judge it."""

    model_config = ConfigDict(extra="forbid")

    source: str
    registered: bool = True
    configured: bool = False
    plan: PullPlanOut
    watermark: WatermarkOut | None = None
    lease: LeaseOut | None = None


class SchedulerStatusOut(BaseModel):
    """The whole schedule in one read: config, per-feed plan, watermark, lease."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    interval_minutes: int
    lookback_days: int
    max_catchup_days: int
    dry_run: bool
    lease_ttl_seconds: int
    today: date
    sources: list[ScheduledSourceOut] = Field(default_factory=list)


class TickOut(BaseModel):
    """One scheduled attempt and its result.

    Carries the ingest counts as well as the batch id, so a cron wrapper can
    alert on "ran but landed nothing" without a second call.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    outcome: TickResult
    reason: PlanReason | None = None
    detail: str = ""
    start: date | None = None
    end: date | None = None
    gap_days: int = 0
    batch_id: str | None = None
    received: int = 0
    created: int = 0
    updated: int = 0
    rejected_count: int = 0
    unresolved_count: int = 0
    error: str | None = None
    duration_ms: int = 0


class TickReportOut(BaseModel):
    """Every attempt from one pass over the configured feeds."""

    model_config = ConfigDict(extra="forbid")

    ticks: list[TickOut] = Field(default_factory=list)
    ran: int = 0
    skipped: int = 0
    lost_lease: int = 0
    failed: int = 0

    @property
    def ok(self) -> bool:
        """Nothing failed. A skip and a lost lease are both a healthy schedule."""
        return self.failed == 0


__all__ = [
    "LeaseOut",
    "PlanReason",
    "PullPlanOut",
    "ScheduledSourceOut",
    "SchedulerStatusOut",
    "TickOut",
    "TickReportOut",
    "TickResult",
    "WatermarkOut",
]
