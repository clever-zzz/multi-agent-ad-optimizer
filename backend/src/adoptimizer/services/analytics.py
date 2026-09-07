"""Read models for the dashboard and reporting endpoints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utc_today
from ..core.logging import get_logger
from ..domain.enums import ActionStatus, AlertStatus, RunStatus
from ..domain.kpi import PerformanceSnapshot, health_score, health_status, summarize
from ..infra.db.models import (
    Alert,
    Campaign,
    DailyMetric,
    LLMSpendRecord,
    OptimizationAction,
    OptimizationRun,
)
from ..repositories.campaigns import MetricRepository

logger = get_logger(__name__)


class AnalyticsService:
    """Aggregations that back the overview, trends and leaderboard views."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._metrics = MetricRepository(session)

    async def overview(self, *, days: int = 7) -> dict[str, Any]:
        """Headline KPIs, health and outstanding work for the landing page."""
        snapshots = await self._metrics.snapshots(None, days=days)
        portfolio = summarize(snapshots)
        score = health_score(portfolio)

        alert_counts = await self._alert_counts()
        action_counts = await self._action_counts()
        run_counts = await self._run_counts()

        return {
            "window_days": days,
            "portfolio": portfolio.model_dump(),
            "health": {"score": round(score, 2), "status": health_status(score)},
            "campaigns": {
                "total": await self._count(Campaign),
                "active": await self._count(Campaign, Campaign.status == "active"),
                "paused": await self._count(Campaign, Campaign.status == "paused"),
            },
            "alerts": alert_counts,
            "actions": action_counts,
            "runs": run_counts,
            "top_campaigns": _rank(snapshots)[:5],
            "worst_campaigns": _rank(snapshots, ascending=True)[:5],
        }

    async def snapshots(
        self, *, days: int = 7, campaign_ids: Sequence[str] | None = None
    ) -> list[PerformanceSnapshot]:
        """Per-campaign performance, ordered by spend.

        Backs the campaign table so the listing can show delivery next to
        configuration without one request per row.
        """
        rows = await self._metrics.snapshots(campaign_ids, days=days)
        return sorted(rows, key=lambda snapshot: -snapshot.total_cost)

    async def timeseries(
        self, *, days: int = 30, campaign_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Daily delivery trend with derived rates."""
        cutoff = utc_today() - timedelta(days=max(1, days) - 1)
        statement: Select[Any] = (
            select(
                DailyMetric.stat_date,
                func.sum(DailyMetric.impressions).label("impressions"),
                func.sum(DailyMetric.clicks).label("clicks"),
                func.sum(DailyMetric.conversions).label("conversions"),
                func.sum(DailyMetric.cost).label("cost"),
                func.sum(DailyMetric.revenue).label("revenue"),
            )
            .where(DailyMetric.stat_date >= cutoff)
            .group_by(DailyMetric.stat_date)
            .order_by(DailyMetric.stat_date.asc())
        )
        if campaign_id:
            statement = statement.where(DailyMetric.campaign_id == campaign_id)

        rows = (await self._session.execute(statement)).all()
        series: list[dict[str, Any]] = []
        for row in rows:
            impressions = int(row.impressions or 0)
            clicks = int(row.clicks or 0)
            conversions = int(row.conversions or 0)
            cost = float(row.cost or 0.0)
            revenue = float(row.revenue or 0.0)
            series.append(
                {
                    "date": row.stat_date.isoformat(),
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": conversions,
                    "cost": round(cost, 2),
                    "revenue": round(revenue, 2),
                    "ctr": round(clicks / impressions, 6) if impressions else 0.0,
                    "cvr": round(conversions / clicks, 6) if clicks else 0.0,
                    "cpa": round(cost / conversions, 2) if conversions else None,
                    "roas": round(revenue / cost, 4) if cost else 0.0,
                }
            )
        return series

    async def campaign_breakdown(self, campaign_id: str, *, days: int = 7) -> dict[str, Any]:
        """Per-campaign detail including its creatives."""
        snapshots = await self._metrics.snapshots([campaign_id], days=days)
        snapshot = snapshots[0] if snapshots else None
        creative_rows = await self._metrics.creative_stats([campaign_id], days=days)
        creatives = creative_rows.get(campaign_id, [])

        scored = []
        for row in creatives:
            from ..domain.scoring import score_performance

            breakdown = score_performance(
                impressions=row["impressions"],
                clicks=row["clicks"],
                conversions=row["conversions"],
                cost=row["cost"],
                revenue=row["revenue"],
            )
            scored.append(
                {**row, "score": round(breakdown.total, 2), "components": breakdown.as_dict()}
            )
        scored.sort(key=lambda item: -item["score"])

        return {
            "campaign_id": campaign_id,
            "snapshot": snapshot.model_dump() if snapshot else None,
            "creatives": scored,
        }

    async def spend_summary(self, *, days: int = 30) -> dict[str, Any]:
        """Model spend grouped by provider and model."""
        cutoff_date = utc_today() - timedelta(days=days)
        statement = (
            select(
                LLMSpendRecord.provider,
                LLMSpendRecord.model,
                func.count(LLMSpendRecord.id).label("calls"),
                func.sum(LLMSpendRecord.prompt_tokens).label("prompt_tokens"),
                func.sum(LLMSpendRecord.completion_tokens).label("completion_tokens"),
                func.sum(LLMSpendRecord.cost_usd).label("cost_usd"),
            )
            .where(func.date(LLMSpendRecord.created_at) >= cutoff_date)
            .group_by(LLMSpendRecord.provider, LLMSpendRecord.model)
        )
        rows = (await self._session.execute(statement)).all()
        return {
            "window_days": days,
            "by_model": [
                {
                    "provider": row.provider,
                    "model": row.model,
                    "calls": int(row.calls or 0),
                    "prompt_tokens": int(row.prompt_tokens or 0),
                    "completion_tokens": int(row.completion_tokens or 0),
                    "cost_usd": round(float(row.cost_usd or 0.0), 4),
                }
                for row in rows
            ],
            "total_cost_usd": round(float(sum(float(r.cost_usd or 0.0) for r in rows)), 4),
            "total_calls": int(sum(int(r.calls or 0) for r in rows)),
        }

    async def _alert_counts(self) -> dict[str, Any]:
        open_statement = (
            select(Alert.severity, func.count(Alert.id))
            .where(Alert.status != AlertStatus.RESOLVED.value)
            .group_by(Alert.severity)
        )
        rows = (await self._session.execute(open_statement)).all()
        by_severity = {str(row[0]): int(row[1]) for row in rows}
        return {
            "open": sum(by_severity.values()),
            "by_severity": by_severity,
            "acknowledged": await self._count(
                Alert, Alert.status == AlertStatus.ACKNOWLEDGED.value
            ),
            "resolved": await self._count(Alert, Alert.status == AlertStatus.RESOLVED.value),
        }

    async def _action_counts(self) -> dict[str, Any]:
        statement = select(OptimizationAction.status, func.count(OptimizationAction.id)).group_by(
            OptimizationAction.status
        )
        rows = (await self._session.execute(statement)).all()
        by_status = {str(row[0]): int(row[1]) for row in rows}
        type_statement = select(
            OptimizationAction.action_type, func.count(OptimizationAction.id)
        ).group_by(OptimizationAction.action_type)
        type_rows = (await self._session.execute(type_statement)).all()
        return {
            "by_status": by_status,
            "by_type": {str(row[0]): int(row[1]) for row in type_rows},
            "pending": by_status.get(ActionStatus.PROPOSED.value, 0),
            "executed": by_status.get(ActionStatus.EXECUTED.value, 0),
        }

    async def _run_counts(self) -> dict[str, Any]:
        statement = select(OptimizationRun.status, func.count(OptimizationRun.id)).group_by(
            OptimizationRun.status
        )
        rows = (await self._session.execute(statement)).all()
        by_status = {str(row[0]): int(row[1]) for row in rows}
        latest = (
            (
                await self._session.execute(
                    select(OptimizationRun).order_by(OptimizationRun.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .first()
        )
        return {
            "by_status": by_status,
            "total": sum(by_status.values()),
            "active": by_status.get(RunStatus.RUNNING.value, 0)
            + by_status.get(RunStatus.PENDING.value, 0),
            "latest_run_id": latest.id if latest else None,
            "latest_run_at": latest.created_at.isoformat() if latest else None,
        }

    async def _count(self, model: Any, *conditions: Any) -> int:
        statement: Select[Any] = select(func.count()).select_from(model)
        if conditions:
            statement = statement.where(*conditions)
        return int(await self._session.scalar(statement) or 0)


def _rank(snapshots: list[PerformanceSnapshot], *, ascending: bool = False) -> list[dict[str, Any]]:
    """Order campaigns by ROAS for leaderboard widgets."""
    ordered = sorted(snapshots, key=lambda s: s.roas, reverse=not ascending)
    return [
        {
            "campaign_id": s.campaign_id,
            "campaign_name": s.campaign_name,
            "roas": s.roas,
            "ctr": s.ctr,
            "cvr": s.cvr,
            "cpa": s.cpa,
            "cost": round(s.total_cost, 2),
            "revenue": round(s.total_revenue, 2),
            "impressions": s.impressions,
            "conversions": s.conversions,
        }
        for s in ordered
    ]
