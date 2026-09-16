"""The stale-run reaper, and the progress signal it is allowed to trust.

The predicate used to be ``created_at < cutoff``, which reaped two things it
should not have: a run that sat in the queue before starting, and a run that was
slow but still working. Because ``mark_finished`` refuses to overwrite a
terminal status, a slow run reaped at the cutoff then lost its real result.
These tests pin the replacement - newest ``run_events`` row, falling back to
``started_at`` then ``created_at``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from adoptimizer.core.config import DatabaseSettings
from adoptimizer.domain.enums import RunStatus
from adoptimizer.infra.db.models import OptimizationRun, RunEvent
from adoptimizer.infra.db.session import Database
from adoptimizer.orchestrator.events import AgentEvent, EventBus
from adoptimizer.services.optimization import (
    REAPER_STALE_AFTER,
    DatabaseEventSink,
    OptimizationService,
)

STALE = REAPER_STALE_AFTER + timedelta(minutes=30)


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await instance.create_all()
    yield instance
    await instance.dispose()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def service(database: Database, bus: EventBus) -> OptimizationService:
    # Only `.database` and `.events` are touched on the reaper path, so a stand-in
    # keeps the test off the full container build.
    container = SimpleNamespace(database=database, events=bus)
    return OptimizationService(container)  # type: ignore[arg-type]


async def _add_run(
    database: Database,
    run_id: str,
    *,
    status: RunStatus,
    age: timedelta,
    last_event_age: timedelta | None = None,
) -> None:
    """Insert a run whose timestamps are backdated, plus one event if asked."""
    created = datetime.now(UTC) - age
    async with database.unit_of_work() as session:
        session.add(
            OptimizationRun(
                id=run_id,
                status=status.value,
                trigger_type="api",
                campaign_ids=["camp_a"],
                parameters={},
                created_at=created,
                updated_at=created,
                started_at=created if status is RunStatus.RUNNING else None,
            )
        )
        if last_event_age is not None:
            session.add(
                RunEvent(
                    id="evt_" + run_id,
                    run_id=run_id,
                    seq=1,
                    agent="monitor",
                    event_type="agent.completed",
                    payload={},
                    created_at=datetime.now(UTC) - last_event_age,
                )
            )
        await session.commit()


async def _status_of(database: Database, run_id: str) -> tuple[str, str | None]:
    async with database.unit_of_work() as session:
        run: Any = await session.get(OptimizationRun, run_id)
        return run.status, run.error_message


class TestProgressSignal:
    async def test_a_run_that_is_still_emitting_events_survives(
        self, database: Database, service: OptimizationService
    ) -> None:
        """The regression: old created_at, but fresh progress."""
        await _add_run(
            database,
            "run_busy",
            status=RunStatus.RUNNING,
            age=STALE,
            last_event_age=timedelta(seconds=5),
        )
        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 0
            await session.commit()
        status, error = await _status_of(database, "run_busy")
        assert status == RunStatus.RUNNING.value
        assert error is None

    async def test_a_silent_run_past_the_window_is_reaped(
        self, database: Database, service: OptimizationService
    ) -> None:
        await _add_run(
            database,
            "run_dead",
            status=RunStatus.RUNNING,
            age=STALE,
            last_event_age=STALE,
        )
        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 1
            await session.commit()
        status, error = await _status_of(database, "run_dead")
        assert status == RunStatus.FAILED.value
        assert error is not None and error.startswith("Reaped:")

    async def test_a_queued_run_with_no_events_falls_back_to_created_at(
        self, database: Database, service: OptimizationService
    ) -> None:
        await _add_run(database, "run_queued", status=RunStatus.PENDING, age=STALE)
        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 1
            await session.commit()
        status, _ = await _status_of(database, "run_queued")
        assert status == RunStatus.FAILED.value

    async def test_a_recent_run_is_left_alone(
        self, database: Database, service: OptimizationService
    ) -> None:
        await _add_run(database, "run_new", status=RunStatus.RUNNING, age=timedelta(seconds=30))
        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 0
            await session.commit()
        status, _ = await _status_of(database, "run_new")
        assert status == RunStatus.RUNNING.value

    async def test_a_finished_run_is_never_reopened(
        self, database: Database, service: OptimizationService
    ) -> None:
        await _add_run(database, "run_done", status=RunStatus.SUCCEEDED, age=STALE)
        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 0
            await session.commit()
        status, _ = await _status_of(database, "run_done")
        assert status == RunStatus.SUCCEEDED.value


class TestTeardown:
    async def test_reaping_closes_the_event_stream(
        self, database: Database, service: OptimizationService, bus: EventBus
    ) -> None:
        """A reaped run must release its subscribers and history like a normal finish."""
        await _add_run(database, "run_orphan", status=RunStatus.RUNNING, age=STALE)
        _subscriber_id, queue = bus.subscribe("run_orphan")

        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 1
            await session.commit()

        assert queue.get_nowait() is None
        assert bus.replay("run_orphan") == []

    async def test_reaping_tells_the_sink_to_forget_the_run(
        self, database: Database, bus: EventBus, service: OptimizationService
    ) -> None:
        """The sink's cache is exactly what a terminal event would have cleared.

        A run whose task died never publishes one - that is the run the reaper
        exists for - so the reaper's teardown is the only thing left that can
        free the entry. The event is backdated because the sink writes it as a
        ``run_events`` row, and a fresh row is the progress signal that would
        otherwise keep the run alive.
        """
        sink = DatabaseEventSink(database.session_factory)
        bus.set_sink(sink)
        await _add_run(database, "run_orphan", status=RunStatus.RUNNING, age=STALE)
        await sink.persist(
            AgentEvent(
                run_id="run_orphan",
                seq=1,
                agent="monitor",
                event_type="agent.completed",
                payload={"iteration": 3},
                created_at=datetime.now(UTC) - STALE,
            )
        )
        assert sink._seen_iterations == {"run_orphan": 3}

        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 1
            await session.commit()

        assert sink._seen_iterations == {}

    async def test_a_sink_that_cannot_forget_does_not_break_teardown(
        self, database: Database, bus: EventBus, service: OptimizationService
    ) -> None:
        """Teardown must survive a sink that fails, since the run is already over."""

        class ExplodingSink:
            async def persist(self, event: AgentEvent) -> None:
                return None

            async def forget(self, run_id: str) -> None:
                raise RuntimeError("the sink is having a day")

        bus.set_sink(ExplodingSink())
        await _add_run(database, "run_orphan", status=RunStatus.RUNNING, age=STALE)

        async with database.unit_of_work() as session:
            assert await service.reap_stale_runs(session) == 1
            await session.commit()

        status, _ = await _status_of(database, "run_orphan")
        assert status == RunStatus.FAILED.value
