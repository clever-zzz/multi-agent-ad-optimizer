"""The scheduled pull, end to end.

``tests/unit/test_scheduling.py`` proves the window arithmetic. These prove the
machinery around it, which is the part that fails silently in production: the
diagnostics endpoint is authorised and cannot start work, a tick writes a
watermark and lets its lease go, a missed run closes itself, a gap wider than the
recovery bound is reported instead of absorbed, two overlapping ticks produce
exactly one pull, and the resident loop stops when it is told to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adoptimizer.core.clock import utc_today
from adoptimizer.core.config import DataMode, IngestSettings
from adoptimizer.infra.ads.registry import build_platform_clients
from adoptimizer.infra.db.models import DailyMetric, IngestBatch, IngestWatermark, SchedulerLease
from adoptimizer.infra.ingest import (
    MetricSourceRegistry,
    SourceRecord,
    SourceTarget,
    build_metric_sources,
)
from adoptimizer.repositories.scheduling import LeaseRepository, WatermarkRepository
from adoptimizer.schemas.scheduling import PlanReason, TickResult
from adoptimizer.services.scheduling import IngestScheduler

from ..conftest import API

SCHEDULE = API + "/ingest/schedule"
FEED = "synthetic"
LEASE = "ingest:" + FEED
HOLDER = "test-holder"
LOOKBACK_DAYS = 3

# The application seed builds 8 pullable campaigns with 21 days of history
# ending today, and the fixture below adds a ninth that has none. Every window a
# test asks for therefore lands as an update for the seeded eight and as a create
# for the probe - which is the distinction worth asserting, because a scheduler
# that reported the seeded days as new rows would be double-counting them.
SEEDED_CAMPAIGNS = 8
PULLABLE_CAMPAIGNS = SEEDED_CAMPAIGNS + 1


def pulled(days: int) -> int:
    """Rows one feed returns for a window of ``days`` days."""
    return PULLABLE_CAMPAIGNS * days


class SlowFeed:
    """A feed that holds its lease open long enough for a rival to arrive.

    Timing a real race would make this suite flaky, and a flaky concurrency test
    teaches people to re-run rather than read. Holding the lease deterministically
    produces the exact interleaving the guard exists for.
    """

    name = "slow.feed"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def is_configured(self) -> bool:
        return True

    async def fetch(
        self, *, start: date, end: date, targets: Sequence[SourceTarget] = ()
    ) -> list[SourceRecord]:
        self.started.set()
        await self.release.wait()
        return []


@pytest.fixture
def sessions(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """The application's own session factory, so a test writes what the app reads."""
    factory: async_sessionmaker[AsyncSession] = app.state.container.database.session_factory
    return factory


@pytest.fixture
def feeds() -> MetricSourceRegistry:
    return build_metric_sources(build_platform_clients(DataMode.MOCK))


@pytest.fixture
def scheduler(
    sessions: async_sessionmaker[AsyncSession], feeds: MetricSourceRegistry
) -> IngestScheduler:
    return build_scheduler(sessions, feeds)


def build_scheduler(
    sessions: async_sessionmaker[AsyncSession],
    feeds: MetricSourceRegistry,
    *,
    holder: str = HOLDER,
    **overrides: Any,
) -> IngestScheduler:
    """A scheduler over the application database with one setting changed."""
    return IngestScheduler(sessions, feeds, IngestSettings(**overrides), holder=holder)


@pytest.fixture
async def campaign(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> dict[str, Any]:
    """One pullable campaign, so a tick lands rows instead of an empty report."""
    created = await client.post(
        API + "/campaigns",
        json={
            "name": "Scheduler Probe",
            "platform": "mock",
            "daily_budget": 500.0,
            "external_id": "ext_scheduler_probe",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


async def watermark(
    sessions: async_sessionmaker[AsyncSession], source: str = FEED
) -> IngestWatermark | None:
    async with sessions() as session:
        return await session.get(IngestWatermark, source)


async def lease(
    sessions: async_sessionmaker[AsyncSession], name: str = LEASE
) -> SchedulerLease | None:
    async with sessions() as session:
        return await session.get(SchedulerLease, name)


async def rows_for(sessions: async_sessionmaker[AsyncSession], campaign_id: str) -> int:
    async with sessions() as session:
        statement = (
            select(func.count())
            .select_from(DailyMetric)
            .where(DailyMetric.campaign_id == campaign_id)
        )
        return int((await session.execute(statement)).scalar_one())


async def batches(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        statement = select(func.count()).select_from(IngestBatch)
        return int((await session.execute(statement)).scalar_one())


async def set_watermark(
    sessions: async_sessionmaker[AsyncSession],
    source: str,
    start: date,
    end: date,
) -> None:
    """Pretend an earlier run covered this span, which is how a gap is simulated."""
    async with sessions() as session:
        session.add(
            IngestWatermark(
                source=source, window_start=start, window_end=end, received=0, dry_run=False
            )
        )
        await session.commit()


async def hold_lease(
    sessions: async_sessionmaker[AsyncSession],
    holder: str,
    ttl: timedelta,
    name: str = LEASE,
) -> bool:
    async with sessions() as session:
        won, _ = await LeaseRepository(session).acquire(name, holder=holder, ttl=ttl)
        await session.commit()
    return won


class TestScheduleEndpoint:
    """Read-only diagnostics: the answer to "why did the numbers not update"."""

    async def test_anonymous_access_is_refused(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(SCHEDULE)).status_code == 401

    async def test_the_read_roles_may_see_the_schedule(
        self, client: httpx.AsyncClient, make_user: Any
    ) -> None:
        for role in ("viewer", "analyst", "optimizer"):
            headers = (await make_user(role))["headers"]
            response = await client.get(SCHEDULE, headers=headers)
            assert response.status_code == 200, role + ": " + response.text

    async def test_the_body_is_the_whole_schedule_in_one_read(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        body = (await client.get(SCHEDULE, headers=admin_headers)).json()

        assert body["enabled"] is True
        assert body["interval_minutes"] == 360
        assert body["lookback_days"] == LOOKBACK_DAYS
        assert body["max_catchup_days"] == 14
        assert body["lease_ttl_seconds"] == 1800
        assert body["dry_run"] is False
        assert body["today"] == utc_today().isoformat()
        assert [entry["source"] for entry in body["sources"]] == [FEED]

    async def test_a_feed_that_has_never_run_plans_a_first_window(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        entry = (await client.get(SCHEDULE, headers=admin_headers)).json()["sources"][0]

        assert entry["registered"] is True
        assert entry["configured"] is True
        assert entry["plan"]["reason"] == "first"
        assert entry["plan"]["days"] == LOOKBACK_DAYS
        assert entry["plan"]["end"] == utc_today().isoformat()
        assert entry["watermark"] is None
        assert entry["lease"] is None

    async def test_polling_it_starts_no_work(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """A dashboard that made the schedule run would be a denial of service."""
        for _ in range(3):
            assert (await client.get(SCHEDULE, headers=admin_headers)).status_code == 200

        assert await watermark(sessions) is None
        assert await batches(sessions) == 0

    async def test_after_a_tick_it_reports_the_window_as_covered(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        scheduler: IngestScheduler,
        campaign: dict[str, Any],
    ) -> None:
        tick = await scheduler.tick(FEED)
        assert tick.outcome is TickResult.RAN

        entry = (await client.get(SCHEDULE, headers=admin_headers)).json()["sources"][0]

        assert entry["plan"]["reason"] == "covered"
        assert entry["watermark"]["window_end"] == utc_today().isoformat()
        assert entry["watermark"]["lag_days"] == 0
        assert entry["watermark"]["batch_id"] == tick.batch_id
        assert entry["watermark"]["received"] == pulled(LOOKBACK_DAYS)
        assert entry["lease"]["held"] is False
        assert entry["lease"]["last_outcome"] == "ran"
        assert entry["lease"]["last_batch_id"] == tick.batch_id
        assert entry["lease"]["consecutive_failures"] == 0

    async def test_a_capped_plan_tells_the_operator_what_to_run(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=40), today - timedelta(days=30))

        entry = (await client.get(SCHEDULE, headers=admin_headers)).json()["sources"][0]

        assert entry["plan"]["reason"] == "capped"
        assert entry["plan"]["gap_days"] == 16
        assert "adoptimizer ingest --start" in entry["plan"]["detail"]
        assert entry["watermark"]["lag_days"] == 30

    async def test_a_typo_in_the_feed_list_is_visible(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
    ) -> None:
        """A name that is not a feed must be reported, not silently skipped."""
        status = await build_scheduler(sessions, feeds, sources=["ghost.feed"]).status()

        entry = status.sources[0]
        assert entry.source == "ghost.feed"
        assert entry.registered is False
        assert entry.configured is False
        assert entry.plan.reason is PlanReason.FIRST
        assert entry.watermark is None
        assert entry.lease is None

    async def test_an_uncredentialed_feed_is_listed_rather_than_hidden(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
    ) -> None:
        """ "Installed but not credentialed" is a state an operator has to be able to see."""
        status = await build_scheduler(sessions, feeds, sources=["platform"]).status()

        assert status.sources[0].registered is True
        assert status.sources[0].configured is False

    async def test_the_status_reports_the_configuration_it_is_running_under(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
    ) -> None:
        instance = build_scheduler(
            sessions,
            feeds,
            scheduler_enabled=False,
            interval_minutes=60,
            lookback_days=5,
            max_catchup_days=30,
            lease_ttl_seconds=900,
            dry_run=True,
        )

        status = await instance.status()

        assert status.enabled is False
        assert status.interval_minutes == 60
        assert status.lookback_days == 5
        assert status.max_catchup_days == 30
        assert status.lease_ttl_seconds == 900
        assert status.dry_run is True
        assert status.today == utc_today()

    async def test_the_status_asks_for_a_day_it_is_given(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
    ) -> None:
        """A pinned day makes the plan reproducible, which is how it is debugged."""
        status = await build_scheduler(sessions, feeds).status(today=date(2026, 1, 10))

        assert status.today == date(2026, 1, 10)
        assert status.sources[0].plan.end == date(2026, 1, 10)
        assert status.sources[0].plan.start == date(2026, 1, 8)


class TestTick:
    """One scheduled attempt: what it writes, and what it refuses to write."""

    async def test_the_first_tick_pulls_and_records_what_it_covered(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        tick = await scheduler.tick(FEED)

        assert tick.outcome is TickResult.RAN
        assert tick.reason is PlanReason.FIRST
        assert tick.received == pulled(LOOKBACK_DAYS)
        assert tick.created == LOOKBACK_DAYS
        assert tick.updated == SEEDED_CAMPAIGNS * LOOKBACK_DAYS
        assert tick.error is None
        assert tick.duration_ms >= 0
        assert await rows_for(sessions, campaign["id"]) == LOOKBACK_DAYS

        row = await watermark(sessions)
        assert row is not None
        assert (row.window_start, row.window_end) == (tick.start, tick.end)
        assert row.batch_id == tick.batch_id
        assert row.received == pulled(LOOKBACK_DAYS)
        assert row.dry_run is False

    async def test_the_second_tick_proves_itself_redundant(
        self, scheduler: IngestScheduler, campaign: dict[str, Any]
    ) -> None:
        await scheduler.tick(FEED)

        second = await scheduler.tick(FEED)

        assert second.outcome is TickResult.SKIPPED
        assert second.reason is PlanReason.COVERED
        assert second.batch_id is None
        assert second.received == 0

    async def test_repulling_updates_rather_than_duplicates(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        """Idempotence is what makes the schedule safe to run from two places."""
        await scheduler.tick(FEED)

        forced = await scheduler.tick(FEED, force=True)

        assert forced.outcome is TickResult.RAN
        assert forced.created == 0
        assert forced.updated == pulled(LOOKBACK_DAYS)
        assert await rows_for(sessions, campaign["id"]) == LOOKBACK_DAYS

    async def test_a_dry_run_writes_nothing_and_advances_nothing(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        rehearsal = await scheduler.tick(FEED, dry_run=True)

        assert rehearsal.outcome is TickResult.RAN
        assert rehearsal.received == pulled(LOOKBACK_DAYS)
        assert await rows_for(sessions, campaign["id"]) == 0
        assert await watermark(sessions) is None
        # The attempt is still recorded: a rehearsal nobody can see is a rehearsal
        # nobody can prove happened.
        assert await batches(sessions) == 1

        real = await scheduler.tick(FEED)

        assert real.reason is PlanReason.FIRST
        assert real.created == LOOKBACK_DAYS

    async def test_the_settings_can_make_every_tick_a_rehearsal(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        """INGEST__DRY_RUN is a deployment-wide switch the CLI can only add to."""
        instance = build_scheduler(sessions, feeds, dry_run=True)

        assert (await instance.tick(FEED)).outcome is TickResult.RAN
        assert await watermark(sessions) is None

    async def test_the_lease_is_let_go_after_a_tick(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        await scheduler.tick(FEED)

        row = await lease(sessions)

        assert row is not None
        assert row.holder == ""
        assert row.expires_at is None
        assert row.last_outcome == "ran"
        assert row.last_batch_id is not None
        assert row.consecutive_failures == 0
        assert row.last_finished_at is not None

    async def test_a_feed_that_cannot_run_is_reported_rather_than_raised(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        """One uncredentialed platform must not stop the feeds queued behind it."""
        instance = build_scheduler(sessions, feeds, sources=["platform"])

        tick = await instance.tick("platform")

        assert tick.outcome is TickResult.FAILED
        assert tick.error is not None
        assert await watermark(sessions, "platform") is None

        row = await lease(sessions, "ingest:platform")
        assert row is not None
        assert row.holder == ""
        assert row.expires_at is None
        assert row.last_outcome == "failed"
        assert row.consecutive_failures == 1

    async def test_a_pass_covers_every_configured_feed(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        instance = build_scheduler(sessions, feeds, sources=[FEED, "platform"])

        report = await instance.tick_all()

        assert [tick.source for tick in report.ticks] == [FEED, "platform"]
        assert report.ran == 1
        assert report.failed == 1
        assert report.skipped == 0
        assert report.lost_lease == 0
        assert report.ok is False

    async def test_a_failure_first_does_not_stop_the_feeds_behind_it(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        instance = build_scheduler(sessions, feeds, sources=["platform", FEED])

        report = await instance.tick_all()

        assert report.ticks[0].outcome is TickResult.FAILED
        assert report.ticks[1].outcome is TickResult.RAN


class TestMissedRuns:
    """The reason the window comes from the calendar and not from a cursor."""

    async def test_a_missed_run_is_closed_by_the_next_one(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=9), today - timedelta(days=5))

        tick = await scheduler.tick(FEED)

        assert tick.reason is PlanReason.CATCHUP
        assert tick.start == today - timedelta(days=4)
        assert tick.end == today
        assert tick.received == pulled(5)
        assert tick.gap_days == 0

        row = await watermark(sessions)
        assert row is not None
        assert (row.window_start, row.window_end) == (today - timedelta(days=9), today)

    async def test_a_gap_wider_than_the_bound_is_reported_not_absorbed(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        instance = build_scheduler(sessions, feeds, max_catchup_days=5)
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=40), today - timedelta(days=30))

        tick = await instance.tick(FEED)

        assert tick.reason is PlanReason.CAPPED
        assert tick.start == today - timedelta(days=4)
        assert tick.end == today
        # The bound narrowed the window, not the feed: five days for every target.
        assert tick.received == pulled(5)
        assert tick.gap_days == 25
        assert "adoptimizer ingest --start" in tick.detail

    async def test_a_capped_tick_still_widens_the_span_it_knows_about(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        """The uncovered days stay owed; the covered ones must not be forgotten."""
        instance = build_scheduler(sessions, feeds, max_catchup_days=5)
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=40), today - timedelta(days=30))

        await instance.tick(FEED)

        row = await watermark(sessions)
        assert row is not None
        assert row.window_start == today - timedelta(days=40)
        assert row.window_end == today

    async def test_a_narrow_catch_up_does_not_erase_a_wide_backfill(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        """Without this the schedule would re-pull history on every tick forever."""
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=90), today - timedelta(days=5))

        tick = await scheduler.tick(FEED)

        assert tick.reason is PlanReason.CATCHUP
        assert tick.start == today - timedelta(days=4)
        row = await watermark(sessions)
        assert row is not None
        assert row.window_start == today - timedelta(days=90)

    async def test_a_backfill_ahead_of_today_needs_no_pull(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=2), today + timedelta(days=1))

        assert (await scheduler.tick(FEED)).outcome is TickResult.SKIPPED


class TestEmptyLookups:
    """Asking for nothing must not become asking for everything."""

    async def test_an_empty_feed_list_reads_no_watermarks(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`IN ()` is a syntax error on PostgreSQL, so the guard is not decoration."""
        async with sessions() as session:
            assert await WatermarkRepository(session).for_sources([]) == {}

    async def test_an_empty_lease_list_reads_no_leases(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            assert await LeaseRepository(session).by_names([]) == {}

    async def test_a_lookup_still_finds_what_it_asked_for(
        self,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        today = utc_today()
        await set_watermark(sessions, FEED, today - timedelta(days=2), today)
        assert await hold_lease(sessions, HOLDER, timedelta(seconds=60)) is True

        async with sessions() as session:
            marks = await WatermarkRepository(session).for_sources([FEED, "never.pulled"])
            leases = await LeaseRepository(session).by_names([LEASE])

        assert set(marks) == {FEED}
        assert marks[FEED].window_end == today
        assert set(leases) == {LEASE}
        assert leases[LEASE].holder == HOLDER


class TestLease:
    """Single flight: exactly one holder pulls a given feed at a time."""

    async def test_a_held_lease_makes_a_second_holder_stand_down(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        assert await hold_lease(sessions, "someone-else", timedelta(seconds=600)) is True

        tick = await scheduler.tick(FEED)

        assert tick.outcome is TickResult.LOST_LEASE
        assert "someone-else" in tick.detail
        assert tick.batch_id is None
        assert await watermark(sessions) is None
        # Standing down is not an error: the data is being covered right now.
        assert (await scheduler.tick_all()).ok is True

    async def test_a_lease_whose_holder_died_is_reclaimable(
        self,
        scheduler: IngestScheduler,
        sessions: async_sessionmaker[AsyncSession],
        campaign: dict[str, Any],
    ) -> None:
        """A crashed pod must not wedge the schedule until somebody intervenes."""
        assert await hold_lease(sessions, "a-pod-that-died", timedelta(seconds=-1)) is True

        tick = await scheduler.tick(FEED)

        assert tick.outcome is TickResult.RAN
        row = await lease(sessions)
        assert row is not None
        assert row.holder == ""

    async def test_a_holder_that_lost_the_lease_cannot_release_it(
        self, sessions: async_sessionmaker[AsyncSession], campaign: dict[str, Any]
    ) -> None:
        """Otherwise two holders could alternately free each other."""
        assert await hold_lease(sessions, "new-holder", timedelta(seconds=600)) is True

        async with sessions() as session:
            released = await LeaseRepository(session).record_outcome(
                LEASE, holder="stale-holder", outcome="ran"
            )
            await session.commit()

        assert released is False
        row = await lease(sessions)
        assert row is not None
        assert row.holder == "new-holder"
        assert row.last_outcome == ""

    async def test_recording_an_outcome_nobody_held_is_a_no_op(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The row is created by acquiring, never by releasing."""
        async with sessions() as session:
            released = await LeaseRepository(session).record_outcome(
                "ingest:ghost", holder="x", outcome="ran"
            )
            await session.commit()

        assert released is False
        assert await lease(sessions, "ingest:ghost") is None

    async def test_an_insert_race_is_settled_by_the_primary_key(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two first-ever holders both try to create the row; one loses cleanly."""
        assert await hold_lease(sessions, "holder-a", timedelta(seconds=600)) is True

        async def nothing(*args: object, **kwargs: object) -> None:
            return None

        # Forces the claim down the insert path, which is where the race lives.
        monkeypatch.setattr(LeaseRepository, "by_name", nothing)

        tick = await build_scheduler(sessions, feeds, holder="holder-b").tick(FEED)

        assert tick.outcome is TickResult.LOST_LEASE
        assert "an unidentified process" in tick.detail

    async def test_two_overlapping_ticks_produce_exactly_one_pull(
        self, sessions: async_sessionmaker[AsyncSession], campaign: dict[str, Any]
    ) -> None:
        feed = SlowFeed()
        registry = MetricSourceRegistry([feed])
        winner = build_scheduler(sessions, registry, holder="holder-a", sources=[SlowFeed.name])
        loser = build_scheduler(sessions, registry, holder="holder-b", sources=[SlowFeed.name])

        first = asyncio.create_task(winner.tick(SlowFeed.name))
        await asyncio.wait_for(feed.started.wait(), timeout=10)
        second = await loser.tick(SlowFeed.name)
        feed.release.set()
        first_tick = await asyncio.wait_for(first, timeout=10)

        assert first_tick.outcome is TickResult.RAN
        assert second.outcome is TickResult.LOST_LEASE
        assert "holder-a" in second.detail

    async def test_losing_the_outcome_record_does_not_fail_a_landed_pull(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lease expires on its own, so the data matters more than the record."""

        async def refuse(*args: object, **kwargs: object) -> bool:
            raise RuntimeError("the database went away mid-tick")

        monkeypatch.setattr(LeaseRepository, "record_outcome", refuse)

        tick = await build_scheduler(sessions, feeds).tick(FEED)

        assert tick.outcome is TickResult.RAN
        assert await watermark(sessions) is not None
        assert await rows_for(sessions, campaign["id"]) == LOOKBACK_DAYS


class TestResidentLoop:
    """The in-process cadence, for deployments without a scheduler of their own."""

    async def test_the_loop_stops_when_it_is_told_to(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        instance = build_scheduler(sessions, feeds, interval_minutes=1)
        stop = asyncio.Event()

        async def halt() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.wait_for(asyncio.gather(instance.run_forever(stop=stop), halt()), timeout=30)

        assert await watermark(sessions) is not None

    async def test_a_loop_told_to_stop_first_never_pulls(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        """Shutdown during start-up must not leave a half-pulled window behind."""
        instance = build_scheduler(sessions, feeds, interval_minutes=1)
        stop = asyncio.Event()
        stop.set()

        await asyncio.wait_for(instance.run_forever(stop=stop), timeout=10)

        assert await watermark(sessions) is None

    async def test_the_interval_elapsing_is_how_the_loop_ticks_over(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The timeout branch is the ordinary one, not the exceptional one.

        The configured minimum interval is a minute, which is too long to wait for
        in a test, so the wait is shortened rather than the assertion weakened.
        """
        instance = build_scheduler(sessions, feeds, interval_minutes=1)
        stop = asyncio.Event()
        elapsed = {"count": 0}
        real_wait_for = asyncio.wait_for

        async def short_wait(awaitable: Any, **kwargs: Any) -> Any:
            """Stands in for asyncio.wait_for, which receives its timeout by keyword."""
            elapsed["count"] += 1
            return await real_wait_for(awaitable, 0.05)

        monkeypatch.setattr(asyncio, "wait_for", short_wait)
        task = asyncio.create_task(instance.run_forever(stop=stop))
        await asyncio.sleep(0.4)
        stop.set()
        await real_wait_for(task, 30)

        assert elapsed["count"] >= 2
        assert await watermark(sessions) is not None

    async def test_a_failing_pass_does_not_stop_the_loop(
        self,
        sessions: async_sessionmaker[AsyncSession],
        feeds: MetricSourceRegistry,
        campaign: dict[str, Any],
    ) -> None:
        instance = build_scheduler(sessions, feeds, sources=["platform"], interval_minutes=1)
        stop = asyncio.Event()

        async def halt() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.wait_for(asyncio.gather(instance.run_forever(stop=stop), halt()), timeout=30)

        row = await lease(sessions, "ingest:platform")
        assert row is not None
        assert row.consecutive_failures >= 1
