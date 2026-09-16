"""Optimization run orchestration.

A run is created and committed before any agent executes, then driven by a
background task with its own database session. That ordering matters: the API
returns a run id immediately, the client streams progress over SSE, and a
crashed worker leaves a durable record that can be retried or marked failed by
the reaper rather than disappearing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents.base import AgentContext, CancellationCheck, RunCancelled
from ..core.config import Settings
from ..core.container import Container
from ..core.errors import ConflictError, NotFoundError, RunInProgressError, ValidationFailure
from ..core.ids import new_id
from ..core.logging import get_logger
from ..core.metrics import ALERTS_TOTAL
from ..domain.anomaly import Alert as DomainAlert
from ..domain.anomaly import AlertThresholds, deduplicate, detect
from ..domain.enums import AlertRule, AlertSeverity, RunStatus
from ..infra.db.models import OptimizationRun, RunEvent
from ..orchestrator.events import TERMINAL_EVENTS, AgentEvent, EventSink
from ..orchestrator.state import (
    AgentState,
    initial_state,
    summarise_state,
    suppressed_action_ids,
)
from ..repositories.alerts import AlertRepository
from ..repositories.runs import RunRepository
from .campaigns import CampaignService

logger = get_logger(__name__)

REAPER_STALE_AFTER = timedelta(minutes=30)

# How often a healthy process re-sweeps. The startup pass alone cannot help the
# case that actually matters: a task that died while the process stayed up.
REAPER_POLL_INTERVAL = timedelta(minutes=10)


class DatabaseEventSink(EventSink):
    """Persists run events using short-lived sessions.

    Also mirrors the run's ``iteration`` onto ``optimization_runs`` as each round
    completes, so the stored row shows live progress instead of only what
    ``mark_finished`` writes at the very end. The last value is cached per run
    and written only when it advances, which keeps the cost at one UPDATE per
    iteration rather than one per event.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._seen_iterations: dict[str, int] = {}

    async def persist(self, event: AgentEvent) -> None:
        async with self._session_factory() as session:
            session.add(
                RunEvent(
                    id=new_id("evt"),
                    run_id=event.run_id,
                    seq=event.seq,
                    agent=event.agent,
                    event_type=event.event_type,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )
            await self._mirror_progress(session, event)
            await session.commit()

    async def _mirror_progress(self, session: AsyncSession, event: AgentEvent) -> None:
        """Copy the round number off the event stream and onto the run row."""
        if event.event_type in TERMINAL_EVENTS:
            self._seen_iterations.pop(event.run_id, None)
            return
        if event.event_type != "agent.completed":
            return
        iteration = int(event.payload.get("iteration", 0) or 0)
        if iteration <= self._seen_iterations.get(event.run_id, 0):
            return
        self._seen_iterations[event.run_id] = iteration
        await RunRepository(session).mark_progress(event.run_id, iteration)


class OptimizationService:
    """Starts, tracks and finalises optimization runs."""

    def __init__(self, container: Container, session: AsyncSession | None = None) -> None:
        self._container = container
        self._session = session
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def settings(self) -> Settings:
        return self._container.settings

    async def start_run(
        self,
        session: AsyncSession,
        *,
        campaign_ids: list[str] | None = None,
        max_iterations: int | None = None,
        window_days: int = 7,
        trigger_type: str = "manual",
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        background: bool = True,
    ) -> OptimizationRun:
        """Validate, persist and dispatch a run."""
        runs = RunRepository(session)
        campaigns = CampaignService(session)

        if idempotency_key and not actor_id:
            # The index that arbitrates a replay is (key, actor). With no actor the
            # pair contains a NULL, SQL treats it as distinct, and the guarantee
            # silently evaporates - so refuse rather than pretend to deduplicate.
            raise ValidationFailure(
                "An Idempotency-Key requires an actor: the key is scoped to whoever sent it"
            )

        if idempotency_key and actor_id:
            existing = await runs.find_by_idempotency_key(idempotency_key, actor_id)
            if existing is not None:
                logger.info("idempotent_run_reused", run_id=existing.id)
                return existing

        cap = self.settings.optimization.max_iterations
        iterations = min(max(1, max_iterations or cap), cap)

        if await runs.active_count() >= self.settings.rate_limit.optimize_runs_per_hour:
            raise RunInProgressError(
                "Too many optimization runs are in flight; wait for the current ones to finish"
            )

        inputs = await campaigns.collect_run_inputs(campaign_ids, days=window_days)
        selected = [str(cid) for cid in inputs["campaign_ids"]]

        if len(selected) > self.settings.optimization.max_campaigns_per_run:
            raise ConflictError(
                "A run may cover at most "
                + str(self.settings.optimization.max_campaigns_per_run)
                + " campaigns"
            )

        try:
            run = await runs.create(
                campaign_ids=selected,
                parameters={
                    "max_iterations": iterations,
                    "window_days": window_days,
                    "campaign_count": len(selected),
                },
                max_iterations=iterations,
                trigger_type=trigger_type,
                requested_by=actor_id,
                idempotency_key=idempotency_key,
            )
            await session.commit()
        except IntegrityError:
            # The lookup above is a read-then-insert, so two concurrent callers can
            # both miss it. The unique index is the arbiter: roll the loser back and
            # hand back the winner's run without dispatching a second execution.
            await session.rollback()
            winner: OptimizationRun | None = None
            if idempotency_key is not None and actor_id is not None:
                winner = await runs.find_by_idempotency_key(idempotency_key, actor_id)
            if winner is None:
                raise
            logger.info("idempotent_run_reused_after_race", run_id=winner.id)
            return winner

        # Always dispatch in-process. `background` only decides whether the caller
        # blocks on completion; skipping dispatch here left synchronous mode with no
        # task to await and `wait_for` raised NotFoundError.
        self._dispatch(run.id, selected, iterations, window_days, actor_id or "system", inputs)
        _ = background
        return run

    def _dispatch(
        self,
        run_id: str,
        campaign_ids: list[str],
        max_iterations: int,
        window_days: int,
        actor: str,
        inputs: dict[str, Any],
    ) -> None:
        """Launch the run as a tracked background task."""
        task = asyncio.create_task(
            self.execute_run(
                run_id,
                campaign_ids=campaign_ids,
                max_iterations=max_iterations,
                window_days=window_days,
                actor=actor,
                inputs=inputs,
            ),
            name="optimize-" + run_id,
        )
        self._tasks[run_id] = task

        def _forget(_finished: asyncio.Task[dict[str, Any]], run_key: str = run_id) -> None:
            self._tasks.pop(run_key, None)

        task.add_done_callback(_forget)

    async def execute_run(
        self,
        run_id: str,
        *,
        campaign_ids: list[str],
        max_iterations: int,
        window_days: int,
        actor: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the graph and persist every result. Safe to call from a worker."""
        bus = self._container.events
        bus.set_sink(DatabaseEventSink(self._container.database.session_factory))

        async with self._container.database.unit_of_work() as session:
            runs = RunRepository(session)
            await runs.mark_running(run_id)
            await session.commit()

        context = AgentContext(
            run_id=run_id,
            optimization=self.settings.optimization,
            gateway=self._container.gateway,
            bus=bus,
            actor=actor,
            tools=self._container.tools,
            snapshots=list((inputs or {}).get("snapshots") or []),
            daily_budgets=dict((inputs or {}).get("daily_budgets") or {}),
            campaign_targets=dict((inputs or {}).get("campaign_targets") or {}),
            existing_creatives=dict((inputs or {}).get("existing_creatives") or {}),
            campaign_refs=dict((inputs or {}).get("campaign_refs") or {}),
            cancellation_check=self._cancellation_check(run_id),
        )

        state: AgentState = initial_state(
            campaign_ids=campaign_ids,
            max_iterations=max_iterations,
            window_days=window_days,
            run_id=run_id,
        )
        context.audience_observations = await self._load_audience_observations(session=None)

        status = RunStatus.SUCCEEDED
        error: str | None = None
        try:
            state = await self._container.orchestrator.run(state, context)
        except RunCancelled:
            status = RunStatus.CANCELLED
            error = "Cancelled by operator"
            logger.info("run_cancelled_cooperatively", run_id=run_id)
        except Exception as exc:
            status = RunStatus.FAILED
            error = str(exc)[:2000]
            logger.error("run_execution_failed", run_id=run_id, error=error)

        return await self._finalise(run_id, state, status=status, error=error)

    def _cancellation_check(self, run_id: str) -> CancellationCheck:
        """Build the cooperative cancellation probe for one run.

        The probe reads the stored status in its own short-lived session, so a
        cancellation issued from another request - or from another API replica
        that does not hold this task - still stops the loop at the next agent
        boundary instead of letting it finish and overwrite the outcome.
        """

        async def _is_closed() -> bool:
            async with self._container.database.unit_of_work() as session:
                stored = await RunRepository(session).status_of(run_id)
            return stored is None or stored.is_terminal

        return _is_closed

    async def _load_audience_observations(self, *, session: AsyncSession | None) -> list[Any]:
        """Fetch demographic slices when a warehouse is configured."""
        if not self.settings.clickhouse.enabled:
            return []
        try:
            async with self._container.database.unit_of_work() as scoped:
                from ..infra.warehouse import build_warehouse

                warehouse = await build_warehouse(scoped)
                if warehouse.name != "clickhouse":
                    return []
                return await warehouse.audience_observations(None, days=7)
        except Exception as exc:
            logger.warning("audience_observations_unavailable", error=str(exc))
            return []

    async def _finalise(
        self, run_id: str, state: AgentState, *, status: RunStatus, error: str | None
    ) -> dict[str, Any]:
        """Persist actions, allocations, alerts and the terminal run state."""
        summary = summarise_state(state, status=status)
        summary["tools"] = self._container.tools.usage(run_id)

        async with self._container.database.unit_of_work() as session:
            runs = RunRepository(session)

            if status == RunStatus.SUCCEEDED:
                # Every proposal is persisted, not just the survivors. A withheld
                # proposal is stored with status `suppressed` so the operator has
                # a row to inspect and overrule; the approval queue still reads
                # only `proposed`, so nothing reaches a platform without review.
                proposed = [
                    action
                    for action in (state.get("optimization_actions") or [])
                    if isinstance(action, dict)
                ]
                findings = [
                    finding
                    for finding in (state.get("critic_findings") or [])
                    if isinstance(finding, dict)
                ]
                allocations = list(state.get("budget_allocations") or [])
                if proposed:
                    await runs.add_actions(
                        run_id, proposed, suppressed_ids=suppressed_action_ids(state)
                    )
                if findings:
                    await runs.add_findings(run_id, findings)
                if allocations:
                    await runs.add_allocations(run_id, allocations)
                await self._persist_alerts(session, state, run_id)
                await self._persist_generated_creatives(session, state, run_id)

            # The gateway owns token accounting, so it is the authority here too.
            # A failed or cancelled run never reaches the supervisor's fold-in,
            # and without this its columns would stay at zero even though the
            # ledger already recorded what the calls cost.
            usage = {
                **dict(state.get("usage") or {}),
                **self._container.gateway.usage(run_id),
            }
            summary["usage"] = usage
            await runs.mark_finished(
                run_id,
                status=status,
                summary=summary,
                iteration=int(state.get("iteration", 0) or 0),
                usage=usage,
                error=error,
            )

        await self._container.events.close_run(run_id)
        await self._container.orchestrator.forget_run(run_id)
        logger.info("run_finalised", run_id=run_id, status=status.value)
        return summary

    async def _persist_alerts(self, session: AsyncSession, state: AgentState, run_id: str) -> None:
        """Store detected alerts and resolve ones that stopped firing."""
        repository = AlertRepository(session)
        raw = list(state.get("alerts") or [])
        domain_alerts: list[DomainAlert] = []
        for item in raw:
            try:
                domain_alerts.append(
                    DomainAlert(
                        campaign_id=str(item["campaign_id"]),
                        # Alerts travel through the graph state as plain dicts, so the
                        # enums have to be rebuilt here; the repository reads `.value`.
                        rule=AlertRule(str(item["rule"])),
                        severity=AlertSeverity(str(item.get("severity", "warning"))),
                        observed=item.get("observed"),
                        threshold=float(item.get("threshold", 0.0) or 0.0),
                        message=str(item.get("message", "")),
                        dedup_key=str(item.get("dedup_key", "")),
                        context=dict(item.get("context") or {}),
                    )
                )
            except (KeyError, ValueError) as exc:
                logger.warning("alert_persist_skipped", error=str(exc))

        # Counted after the in-pass fingerprint collapse, so the series tracks
        # distinct findings per rule rather than every duplicate the detector
        # emitted. Cross-iteration repeats never reach here twice either --
        # ``record_many`` upserts on ``dedup_key``.
        deduplicated = deduplicate(domain_alerts)
        for alert in deduplicated:
            ALERTS_TOTAL.labels(rule=alert.rule.value, severity=alert.severity.value).inc()

        created = await repository.record_many(deduplicated, run_id=run_id)
        logger.info("alerts_persisted", run_id=run_id, total=len(domain_alerts), new=created)

    async def _persist_generated_creatives(
        self, session: AsyncSession, state: AgentState, run_id: str
    ) -> None:
        """Save agent-generated copy as draft creatives for review."""
        from ..infra.db.models import Creative

        variants = list(state.get("new_creatives") or [])
        if not variants:
            return

        records = []
        for variant in variants:
            campaign_id = str(variant.get("campaign_id", ""))
            if not campaign_id:
                continue
            records.append(
                Creative(
                    id=new_id("cre"),
                    campaign_id=campaign_id,
                    headline=str(variant.get("headline", ""))[:300],
                    description=str(variant.get("description", ""))[:2000],
                    cta_text=str(variant.get("cta_text", "Learn More"))[:60],
                    target_emotion=str(variant.get("target_emotion", ""))[:40],
                    ab_group=str(variant.get("ab_group", "variant"))[:40],
                    origin=str(variant.get("source", "rule")),
                    status="draft",
                    generated_by_run_id=run_id,
                    creative_type="text",
                )
            )
        if records:
            session.add_all(records)
            logger.info("generated_creatives_persisted", run_id=run_id, count=len(records))

    async def get_run(self, session: AsyncSession, run_id: str) -> OptimizationRun:
        return await RunRepository(session).get_or_raise(run_id)

    async def run_detail(self, session: AsyncSession, run_id: str) -> dict[str, Any]:
        """Everything the UI needs to render one run."""
        runs = RunRepository(session)
        run = await runs.get_or_raise(run_id)
        events = await runs.events(run_id, limit=1000)
        actions = await runs.actions_for_run(run_id)
        allocations = await runs.allocations_for_run(run_id)
        findings = await runs.findings_for_run(run_id)

        return {
            "run": run,
            "events": events,
            "actions": actions,
            "allocations": allocations,
            "findings": findings,
        }

    async def cancel(
        self, session: AsyncSession, run_id: str, *, actor_id: str | None
    ) -> OptimizationRun:
        """Cancel a queued or executing run."""
        runs = RunRepository(session)
        run = await runs.get_or_raise(run_id)
        if RunStatus(run.status).is_terminal:
            raise ConflictError("This run has already finished with status " + run.status)

        task = self._tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()

        run.status = RunStatus.CANCELLED.value
        run.finished_at = datetime.now(UTC)
        run.error_message = "Cancelled by " + (actor_id or "operator")
        await session.flush()
        await self._container.events.close_run(run_id)
        logger.info("run_cancelled", run_id=run_id, actor=actor_id)
        return run

    async def reap_stale_runs(self, session: AsyncSession) -> int:
        """Mark runs that stopped making progress as failed.

        Progress is the newest ``run_events`` row for the run, falling back to
        ``started_at`` and then ``created_at``. The durable sink already writes
        that row on every agent step, so this needs no extra heartbeat write -
        and unlike ``created_at`` alone it neither reaps a run that merely waited
        in the queue, nor one that is slow but still emitting events. The old
        predicate did both, and because ``mark_finished`` refuses to overwrite a
        terminal status, a slow run reaped at the cutoff then lost its real
        result on completion.

        Called on startup and from ``run_reaper_loop`` so a crashed process never
        leaves the queue permanently blocked.
        """
        runs = RunRepository(session)
        cutoff = datetime.now(UTC) - REAPER_STALE_AFTER
        last_event = (
            select(func.max(RunEvent.created_at))
            .where(RunEvent.run_id == OptimizationRun.id)
            .correlate(OptimizationRun)
            .scalar_subquery()
        )
        progress = func.coalesce(last_event, OptimizationRun.started_at, OptimizationRun.created_at)
        stale, _ = await runs.list(
            filters=[
                OptimizationRun.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value]),
                progress < cutoff,
            ]
        )
        for run in stale:
            run.status = RunStatus.FAILED.value
            run.finished_at = datetime.now(UTC)
            run.error_message = "Reaped: no progress recorded within the stale-run window"
        await session.flush()
        for run in stale:
            # The same teardown a normal finish gets, so a reaped run does not
            # leave its event history and subscribers behind in this process.
            await self._container.events.close_run(run.id)
        if stale:
            logger.warning("stale_runs_reaped", count=len(stale))
        return len(stale)

    async def run_reaper_loop(self, interval: timedelta = REAPER_POLL_INTERVAL) -> None:
        """Re-sweep for stale runs until cancelled.

        The startup pass cannot help the case that matters most: a task that died
        while the process stayed healthy. Each sweep takes its own session and
        swallows its own errors, so one bad sweep cannot kill the loop. Replicas
        may sweep concurrently; they write the same terminal status, so the only
        thing they race over is ``finished_at``.
        """
        while True:
            await asyncio.sleep(interval.total_seconds())
            try:
                async with self._container.database.unit_of_work() as session:
                    reaped = await self.reap_stale_runs(session)
                    await session.commit()
                if reaped:
                    logger.warning("periodic_reaped_stale_runs", count=reaped)
            except Exception as exc:  # pragma: no cover - the loop must survive
                logger.error("reaper_sweep_failed", error=str(exc))

    async def detect_alerts_now(
        self, session: AsyncSession, *, campaign_ids: list[str] | None = None, days: int = 7
    ) -> list[dict[str, Any]]:
        """Run detection outside a full optimization loop (for the alerts API)."""
        campaigns = CampaignService(session)
        snapshots = await campaigns.snapshots(campaign_ids, days=days)
        budgets = await campaigns.daily_budgets(campaign_ids)
        optimization = self.settings.optimization
        thresholds = AlertThresholds(
            ctr_floor=optimization.alert_ctr_floor,
            cpa_ceiling=optimization.alert_cpa_ceiling,
            roas_floor=optimization.alert_roas_floor,
            min_impressions=optimization.min_impressions_for_alerts,
            burn_rate_multiplier=optimization.burn_rate_multiplier,
            burn_rate_critical_multiplier=optimization.burn_rate_critical_multiplier,
            burn_rate_hysteresis=optimization.burn_rate_hysteresis,
        )
        alerts = deduplicate(detect(snapshots, thresholds, daily_budgets=budgets, window_days=days))
        return [alert.to_dict() for alert in alerts]

    # This is the implementation of the wait, not a coroutine that should be
    # wrapped in asyncio.timeout() by its caller, which is what ASYNC109 assumes.
    async def wait_for(self, run_id: str, *, timeout: float = 300.0) -> bool:  # noqa: ASYNC109
        """Block until a run reaches a terminal state. Used by the API and the CLI.

        Whoever dispatched the run awaits its own task. Everybody else has no task
        to await - a concurrent replay of the same idempotency key, a request that
        landed on another replica, a CLI invoked after the fact - and used to get a
        404 for a run that plainly exists. Those callers poll the row instead.
        """
        task = self._tasks.get(run_id)
        if task is None:
            return await self._poll_until_terminal(run_id, timeout=timeout)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # Same reasoning as wait_for above: this implements the wait rather than
    # accepting a task to wrap, so ASYNC109 does not apply.
    async def _poll_until_terminal(self, run_id: str, *, timeout: float) -> bool:  # noqa: ASYNC109
        """Watch the persisted status until the run settles or the budget runs out.

        Each look uses its own short-lived session. Reusing the caller's session
        would pin a transaction open for the whole wait, and that snapshot predates
        the worker's commit - the run would read as pending forever.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        interval = 0.05
        while True:
            async with self._container.database.unit_of_work() as session:
                run = await session.get(OptimizationRun, run_id)
            if run is None:
                raise NotFoundError("Run " + run_id + " does not exist")
            if RunStatus(run.status).is_terminal:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, 1.0)
