"""Optimization run persistence, including its event stream and outputs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select

from ..core.ids import new_id
from ..core.logging import get_logger
from ..core.metrics import ACTIONS_TOTAL
from ..domain.enums import ActionStatus, RunStatus
from ..infra.db.models import (
    BudgetAllocationRecord,
    CriticFinding,
    OptimizationAction,
    OptimizationRun,
    RunEvent,
)
from ..infra.db.session import after_commit
from ..orchestrator.events import AgentEvent
from .base import BaseRepository

logger = get_logger(__name__)


class RunRepository(BaseRepository[OptimizationRun]):
    model = OptimizationRun
    resource_name = "optimization_run"

    async def create(
        self,
        *,
        campaign_ids: Sequence[str],
        parameters: dict[str, Any],
        max_iterations: int,
        trigger_type: str = "manual",
        requested_by: str | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> OptimizationRun:
        """Persist a pending run before any agent executes."""
        run = OptimizationRun(
            id=run_id or new_id("run"),
            status=RunStatus.PENDING.value,
            trigger_type=trigger_type,
            requested_by=requested_by,
            campaign_ids=list(campaign_ids),
            parameters=parameters,
            max_iterations=max_iterations,
            idempotency_key=idempotency_key,
        )
        return await self.add(run)

    async def mark_running(self, run_id: str) -> OptimizationRun:
        """Transition a run into the executing state."""
        run = await self.get_or_raise(run_id)
        run.status = RunStatus.RUNNING.value
        run.started_at = datetime.now(UTC)
        await self.flush()
        return run

    async def mark_progress(self, run_id: str, iteration: int) -> None:
        """Record live iteration progress on a run that is still executing.

        Without this the stored ``iteration`` only appears at ``mark_finished``,
        so an operator watching a long run sees zero until it ends. The write is
        refused once the run is terminal - a late progress report must not
        resurrect a cancelled or reaped run - and refused when it would not
        advance, which keeps it to one UPDATE per iteration.
        """
        run = await self.get_or_raise(run_id)
        if RunStatus(run.status).is_terminal:
            return
        if iteration <= run.iteration:
            return
        run.iteration = iteration
        await self.flush()

    async def mark_finished(
        self,
        run_id: str,
        *,
        status: RunStatus,
        summary: dict[str, Any],
        iteration: int,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> OptimizationRun:
        """Record the terminal state of a run.

        A run that is already terminal keeps its stored status. This happens when
        an operator cancelled it, possibly from another API replica, while the
        executing task was still working; letting the late result win would
        resurrect a cancelled run as succeeded.
        """
        run = await self.get_or_raise(run_id)
        stored = RunStatus(run.status)
        if stored.is_terminal and stored is not status:
            logger.warning(
                "run_terminal_state_preserved",
                run_id=run_id,
                stored=stored.value,
                attempted=status.value,
            )
            return run
        run.status = status.value
        run.finished_at = datetime.now(UTC)
        run.summary = summary
        run.iteration = iteration
        run.error_message = error
        if usage:
            run.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            run.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            run.llm_cost_usd = float(usage.get("cost_usd", 0.0) or 0.0)
        await self.flush()
        return run

    async def status_of(self, run_id: str) -> RunStatus | None:
        """Current stored status, or None when the run does not exist.

        Reads a single column so the cooperative cancellation check between
        agent steps stays cheap.
        """
        statement = select(OptimizationRun.status).where(OptimizationRun.id == run_id)
        value = (await self.session.execute(statement)).scalar_one_or_none()
        return RunStatus(value) if value is not None else None

    async def append_event(self, event: AgentEvent) -> RunEvent:
        """Store one event of the run timeline."""
        record = RunEvent(
            id=new_id("evt"),
            run_id=event.run_id,
            seq=event.seq,
            agent=event.agent,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )
        # The run aggregate owns its child rows, and the generic add() inherited
        # from BaseRepository is typed for OptimizationRun only.
        self.session.add(record)
        return record

    async def events(self, run_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[RunEvent]:
        """Replay the stored timeline, optionally resuming after a sequence."""
        statement = (
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def add_actions(
        self,
        run_id: str,
        actions: Sequence[dict[str, Any]],
        *,
        suppressed_ids: set[str] | None = None,
    ) -> list[OptimizationAction]:
        """Persist proposed actions produced by the optimize agent.

        ``suppressed_ids`` are the proposals the critic withheld. They are stored
        with status ``suppressed`` rather than dropped, which is what gives an
        operator a row to look at - and to overrule - instead of a proposal that
        only ever existed inside the run's process memory.
        """
        withheld = suppressed_ids or set()
        records = [
            OptimizationAction(
                id=str(action.get("id") or new_id("act")),
                run_id=run_id,
                campaign_id=str(action["campaign_id"]),
                creative_id=action.get("creative_id"),
                action_type=str(action["action_type"]),
                status=(
                    ActionStatus.SUPPRESSED.value
                    if str(action.get("id") or "") in withheld
                    else str(action.get("status", ActionStatus.PROPOSED.value))
                ),
                before_value=str(action.get("before_value", "")),
                after_value=str(action.get("after_value", "")),
                reason=str(action.get("reason", "")),
                confidence=float(action.get("confidence", 0.0) or 0.0),
                direction=str(action.get("direction", "") or ""),
                basis=dict(action.get("basis") or {}),
                proposed_by=str(action.get("proposed_by", "optimize")),
            )
            for action in actions
        ]
        self.session.add_all(records)
        # ``outcome`` is the status the row is being written with, so the series
        # splits proposals from the critic's suppressed ones at the moment that
        # distinction is decided. Execution outcomes are counted where they
        # happen, in ``ActionService.execute``.
        #
        # Queued rather than counted here, because ``add_all`` only stages: the
        # caller owns the commit, and a rolled-back run wrote no proposals. The
        # pairs are snapshotted now - ``record.status`` is mutable and the hook
        # runs after the flush that could have changed it.
        proposed = [(record.action_type, record.status) for record in records]

        def count_proposed() -> None:
            for action_type, outcome in proposed:
                ACTIONS_TOTAL.labels(action_type=action_type, outcome=outcome).inc()

        after_commit(self.session, count_proposed)
        return records

    async def add_findings(
        self, run_id: str, findings: Sequence[dict[str, Any]]
    ) -> list[CriticFinding]:
        """Persist the critic's reconciliation verdicts for the audit trail."""
        records = [
            CriticFinding(
                id=str(finding.get("id") or new_id("cfd")),
                run_id=run_id,
                iteration=int(finding.get("iteration", 0) or 0),
                kind=str(finding.get("kind", "")),
                scope=str(finding.get("scope", "campaign")),
                campaign_id=str(finding.get("campaign_id", "")),
                creative_id=finding.get("creative_id") or None,
                kept_action_id=finding.get("kept_action_id") or None,
                kept_action_type=str(finding.get("kept_action_type", "")),
                kept_confidence=float(finding.get("kept_confidence", 0.0) or 0.0),
                reason=str(finding.get("reason", "")),
                suppressed_action_ids=list(finding.get("suppressed_action_ids") or []),
                suppressed_actions=list(finding.get("suppressed_actions") or []),
                escalate=bool(finding.get("escalate", False)),
            )
            for finding in findings
        ]
        self.session.add_all(records)
        return records

    async def findings_for_run(self, run_id: str) -> list[CriticFinding]:
        """Every critic verdict recorded for one run, oldest first."""
        statement = (
            select(CriticFinding)
            .where(CriticFinding.run_id == run_id)
            .order_by(CriticFinding.iteration.asc(), CriticFinding.created_at.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def add_allocations(
        self, run_id: str, allocations: Sequence[dict[str, Any]]
    ) -> list[BudgetAllocationRecord]:
        """Persist the budget reallocation plan."""
        records = [
            BudgetAllocationRecord(
                id=new_id("bud"),
                run_id=run_id,
                campaign_id=str(item["campaign_id"]),
                current_budget=float(item.get("current_budget", 0.0) or 0.0),
                recommended_budget=float(item.get("recommended_budget", 0.0) or 0.0),
                score=float(item.get("score", 0.0) or 0.0),
                change_pct=float(item.get("change_pct", 0.0) or 0.0),
                reason=str(item.get("reason", "")),
                solver=str(item.get("solver", "greedy_lp")),
            )
            for item in allocations
        ]
        self.session.add_all(records)
        return records

    async def actions_for_run(self, run_id: str) -> list[OptimizationAction]:
        """Every action proposed by one run."""
        statement = (
            select(OptimizationAction)
            .where(OptimizationAction.run_id == run_id)
            .order_by(OptimizationAction.confidence.desc(), OptimizationAction.created_at.asc())
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def allocations_for_run(self, run_id: str) -> list[BudgetAllocationRecord]:
        """The persisted budget plan for one run."""
        statement = select(BudgetAllocationRecord).where(BudgetAllocationRecord.run_id == run_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def active_count(self) -> int:
        """How many runs are pending or executing right now."""
        statement = (
            select(func.count())
            .select_from(OptimizationRun)
            .where(OptimizationRun.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value]))
        )
        return int(await self.session.scalar(statement) or 0)

    async def recent(
        self, *, limit: int = 20, status: RunStatus | None = None
    ) -> list[OptimizationRun]:
        """Most recent runs for the dashboard."""
        statement: Select[Any] = select(OptimizationRun).order_by(OptimizationRun.created_at.desc())
        if status is not None:
            statement = statement.where(OptimizationRun.status == status.value)
        return list((await self.session.execute(statement.limit(limit))).scalars().all())

    async def find_by_idempotency_key(self, key: str, actor_id: str) -> OptimizationRun | None:
        """Return a previous run started with the same idempotency key."""
        statement = select(OptimizationRun).where(
            OptimizationRun.idempotency_key == key,
            OptimizationRun.requested_by == actor_id,
        )
        return (await self.session.execute(statement)).scalars().first()


class ActionRepository(BaseRepository[OptimizationAction]):
    model = OptimizationAction
    resource_name = "optimization_action"

    async def pending(self, *, limit: int = 100) -> list[OptimizationAction]:
        """Actions awaiting a human decision."""
        statement = (
            select(OptimizationAction)
            .where(OptimizationAction.status == ActionStatus.PROPOSED.value)
            .order_by(OptimizationAction.confidence.desc(), OptimizationAction.created_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def for_run(self, run_id: str, *, limit: int = 500) -> list[OptimizationAction]:
        """Actions proposed by a specific run."""
        statement = (
            select(OptimizationAction)
            .where(OptimizationAction.run_id == run_id)
            .order_by(OptimizationAction.confidence.desc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())

    async def by_status(
        self, status: ActionStatus, *, limit: int = 200
    ) -> list[OptimizationAction]:
        statement = (
            select(OptimizationAction)
            .where(OptimizationAction.status == status.value)
            .order_by(OptimizationAction.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(statement)).scalars().all())
