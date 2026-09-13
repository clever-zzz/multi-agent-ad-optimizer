"""Database-backed audit trail for tool invocations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.ids import new_id
from ..core.logging import get_logger
from ..infra.db.models import ToolInvocation
from .executor import ToolAuditSink
from .spec import ToolResult

logger = get_logger(__name__)


class DatabaseToolAudit(ToolAuditSink):
    """Persists every invocation, refusals included.

    Writing the audit row must never be the reason a run fails, so ``record``
    swallows its own errors after logging them. Losing an audit line is bad;
    taking the optimizer down over it is worse.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, result: ToolResult) -> None:
        spec = result.spec
        request = result.request
        row = ToolInvocation(
            id=new_id("tool"),
            run_id=request.run_id or None,
            agent=request.agent.value if request.agent is not None else "",
            actor=request.actor,
            tool=request.tool,
            outcome=result.outcome.value,
            read_only=bool(spec.read_only) if spec is not None else True,
            dry_run=result.dry_run,
            touches_platform=bool(spec.touches_platform) if spec is not None else False,
            arguments=dict(request.arguments),
            result=dict(result.data),
            error=result.error,
            duration_ms=round(result.duration_ms),
            idempotency_key=request.idempotency_key,
        )
        try:
            async with self._session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:
            # Audit is best-effort by design: losing a line is bad, taking the
            # optimizer down over it is worse.
            logger.warning("tool_audit_write_failed", tool=request.tool, error=str(exc))

    async def for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Every invocation of one run, oldest first."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ToolInvocation)
                    .where(ToolInvocation.run_id == run_id)
                    .order_by(ToolInvocation.created_at, ToolInvocation.id)
                )
            ).scalars()
        return [_as_dict(row) for row in rows]

    async def summary(self, *, run_id: str | None = None, days: int = 30) -> dict[str, Any]:
        """Counts by tool and outcome, for the admin surface."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            query = (
                select(
                    ToolInvocation.tool,
                    ToolInvocation.outcome,
                    ToolInvocation.dry_run,
                    func.count(ToolInvocation.id).label("calls"),
                    func.avg(ToolInvocation.duration_ms).label("avg_duration_ms"),
                )
                .where(ToolInvocation.created_at >= cutoff)
                .group_by(ToolInvocation.tool, ToolInvocation.outcome, ToolInvocation.dry_run)
            )
            if run_id:
                query = query.where(ToolInvocation.run_id == run_id)
            rows = (await session.execute(query)).all()

        by_tool: dict[str, dict[str, int]] = {}
        totals = {"calls": 0, "dry_run": 0, "refused": 0, "failed": 0}
        refused = {"permission_denied", "budget_exhausted", "validation_failed", "disabled"}
        for row in rows:
            calls = int(row.calls or 0)
            entry = by_tool.setdefault(str(row.tool), {"calls": 0, "dry_run": 0, "failed": 0})
            entry["calls"] += calls
            if row.dry_run:
                entry["dry_run"] += calls
                totals["dry_run"] += calls
            if row.outcome == "failed":
                entry["failed"] += calls
                totals["failed"] += calls
            if row.outcome in refused:
                totals["refused"] += calls
            totals["calls"] += calls

        return {
            "window_days": days,
            "run_id": run_id,
            "totals": totals,
            "by_tool": [{"tool": tool, **counts} for tool, counts in sorted(by_tool.items())],
        }


def _as_dict(row: ToolInvocation) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "agent": row.agent,
        "actor": row.actor,
        "tool": row.tool,
        "outcome": row.outcome,
        "read_only": row.read_only,
        "dry_run": row.dry_run,
        "touches_platform": row.touches_platform,
        "arguments": row.arguments,
        "result": row.result,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
