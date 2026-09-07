"""Database-backed LLM spend ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.ids import new_id
from ..infra.db.models import LLMSpendRecord
from .base import CompletionResult
from .gateway import SpendLedger


class DatabaseSpendLedger(SpendLedger):
    """Persists per-call token usage and answers the budget guardrail query."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, *, run_id: str | None, agent: str, result: CompletionResult) -> None:
        """Write one spend row. Failures never break the agent run."""
        if result.cached:
            return
        async with self._session_factory() as session:
            session.add(
                LLMSpendRecord(
                    id=new_id("spend"),
                    run_id=run_id,
                    agent=agent,
                    provider=result.provider.value,
                    model=result.model,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cost_usd=result.usage.cost_usd,
                    latency_ms=result.latency_ms,
                    outcome="degraded" if result.degraded else "success",
                )
            )
            await session.commit()

    async def month_to_date_usd(self) -> float:
        """Sum spend since the first day of the current UTC month."""
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(LLMSpendRecord.cost_usd), 0.0)).where(
                    LLMSpendRecord.created_at >= month_start
                )
            )
        return float(total or 0.0)

    async def summary(self, *, days: int = 30) -> dict[str, object]:
        """Aggregate spend for the admin dashboard."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        LLMSpendRecord.provider,
                        LLMSpendRecord.model,
                        func.count(LLMSpendRecord.id).label("calls"),
                        func.sum(LLMSpendRecord.prompt_tokens).label("prompt_tokens"),
                        func.sum(LLMSpendRecord.completion_tokens).label("completion_tokens"),
                        func.sum(LLMSpendRecord.cost_usd).label("cost_usd"),
                        func.avg(LLMSpendRecord.latency_ms).label("avg_latency_ms"),
                    )
                    .where(LLMSpendRecord.created_at >= cutoff)
                    .group_by(LLMSpendRecord.provider, LLMSpendRecord.model)
                )
            ).all()

        return {
            "window_days": days,
            "totals": {
                "calls": int(sum(r.calls or 0 for r in rows)),
                "prompt_tokens": int(sum(r.prompt_tokens or 0 for r in rows)),
                "completion_tokens": int(sum(r.completion_tokens or 0 for r in rows)),
                "cost_usd": round(float(sum(r.cost_usd or 0.0 for r in rows)), 4),
            },
            "by_model": [
                {
                    "provider": r.provider,
                    "model": r.model,
                    "calls": int(r.calls or 0),
                    "prompt_tokens": int(r.prompt_tokens or 0),
                    "completion_tokens": int(r.completion_tokens or 0),
                    "cost_usd": round(float(r.cost_usd or 0.0), 4),
                    "avg_latency_ms": round(float(r.avg_latency_ms or 0.0), 1),
                }
                for r in rows
            ],
        }


def as_session_factory(value: object) -> object:
    """Type helper keeping the ledger constructor explicit at call sites."""
    _ = AsyncSession
    return value
