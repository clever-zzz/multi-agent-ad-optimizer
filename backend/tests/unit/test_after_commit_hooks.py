"""Counters describing what landed must move after the commit, not before.

``optimization_actions_total`` used to be incremented while ``add_actions`` was
still only staging rows, so a run that rolled back reported proposals it never
wrote. The inflation is invisible from the metric itself - the series carries no
run id to reconcile against - and it reads as a busy system rather than a broken
one, which is the worst shape a wrong number can have. ``after_commit`` is the
primitive that fixes it; these tests pin the primitive and the call site whose
regression actually cost something.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from adoptimizer.core.config import DatabaseSettings
from adoptimizer.core.metrics import REGISTRY
from adoptimizer.domain.enums import ActionStatus, ActionType, RunStatus
from adoptimizer.infra.db.models import OptimizationRun
from adoptimizer.infra.db.session import Database, after_commit
from adoptimizer.repositories.runs import RunRepository

RUN_ID = "run_after_commit_test"
BUDGET = ActionType.ADJUST_BUDGET.value


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """A private in-memory SQLite schema; nothing external is involved."""
    instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await instance.create_all()
    yield instance
    await instance.dispose()


def proposals(outcome: str = ActionStatus.PROPOSED.value) -> float:
    """One series of the action counter, read as a delta by every test here.

    The registry is process-global, so an absolute assertion would pass or fail
    depending on which tests ran first.
    """
    return (
        REGISTRY.get_sample_value(
            "optimization_actions_total", {"action_type": BUDGET, "outcome": outcome}
        )
        or 0.0
    )


def add_run(session: Any) -> None:
    """The row the proposals hang off, so the foreign key resolves."""
    session.add(
        OptimizationRun(
            id=RUN_ID,
            status=RunStatus.RUNNING.value,
            trigger_type="api",
            campaign_ids=["camp_a", "camp_b"],
            parameters={},
        )
    )


async def stage_two_proposals(session: Any) -> None:
    """Stage - not commit - two budget proposals."""
    add_run(session)
    await RunRepository(session).add_actions(
        RUN_ID,
        [
            {"campaign_id": "camp_a", "action_type": BUDGET},
            {"campaign_id": "camp_b", "action_type": BUDGET},
        ],
    )


class TestAfterCommitHook:
    """The primitive: fires on commit, dies on rollback, never breaks either."""

    async def test_a_queued_hook_runs_once_the_commit_lands(self, database: Database) -> None:
        fired: list[str] = []
        async with database.unit_of_work() as session:
            after_commit(session, lambda: fired.append("committed"))
            assert fired == [], "queueing must not run anything by itself"
        assert fired == ["committed"]

    async def test_a_rollback_discards_the_queue_rather_than_deferring_it(
        self, database: Database
    ) -> None:
        """Dropped, not postponed.

        A session that rolls back and is then reused would otherwise fire the
        stale hooks at its next commit, reporting writes from the abandoned
        attempt - the original bug, moved one transaction later.

        The row is staged first, mirroring every real call site: hooks are
        queued next to a write, and that write is what opens the transaction
        the rollback then ends. ``Session.rollback()`` with nothing pending is
        documented as a pass-through, so it emits no event and leaves a queue
        that was never describing anything.
        """
        fired: list[str] = []
        session = database.session()
        try:
            add_run(session)
            after_commit(session, lambda: fired.append("committed"))
            await session.rollback()
            assert fired == []
            await session.commit()
            assert fired == [], "the abandoned attempt must not be reported later"
            assert await session.get(OptimizationRun, RUN_ID) is None, (
                "and the row it described is gone too, so counter and table agree"
            )
        finally:
            await session.close()

    async def test_a_broken_hook_neither_undoes_the_commit_nor_skips_the_rest(
        self, database: Database
    ) -> None:
        """The transaction is already over, so a bad hook may not affect it."""
        fired: list[str] = []

        def explode() -> None:
            raise RuntimeError("the registry is having a day")

        async with database.unit_of_work() as session:
            add_run(session)
            after_commit(session, explode)
            after_commit(session, lambda: fired.append("second"))

        assert fired == ["second"]
        async with database.unit_of_work() as session:
            assert await session.get(OptimizationRun, RUN_ID) is not None


class TestProposalCounting:
    """The call site that was wrong, asserted through the public repository."""

    async def test_a_committed_run_counts_exactly_what_it_wrote(self, database: Database) -> None:
        before = proposals()
        async with database.unit_of_work() as session:
            await stage_two_proposals(session)
            assert proposals() == before, "staged rows are not written rows"
        assert proposals() == before + 2

    async def test_a_rolled_back_run_reports_no_proposals(self, database: Database) -> None:
        """The regression: a run that never committed used to count anyway."""
        before = proposals()
        with pytest.raises(RuntimeError):
            async with database.unit_of_work() as session:
                await stage_two_proposals(session)
                raise RuntimeError("the run died before it could commit")
        assert proposals() == before

    async def test_deferring_keeps_the_proposed_and_suppressed_split(
        self, database: Database
    ) -> None:
        """``outcome`` is decided at staging time and must survive the delay."""
        before_kept = proposals()
        before_held = proposals(ActionStatus.SUPPRESSED.value)
        async with database.unit_of_work() as session:
            add_run(session)
            await RunRepository(session).add_actions(
                RUN_ID,
                [
                    {"id": "act_kept", "campaign_id": "camp_a", "action_type": BUDGET},
                    {"id": "act_held", "campaign_id": "camp_b", "action_type": BUDGET},
                ],
                suppressed_ids={"act_held"},
            )
        assert proposals() == before_kept + 1
        assert proposals(ActionStatus.SUPPRESSED.value) == before_held + 1
