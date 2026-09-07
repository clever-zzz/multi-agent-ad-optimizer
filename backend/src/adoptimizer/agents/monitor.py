"""Monitor Agent - telemetry collection, anomaly detection and health scoring."""

from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from ..domain.anomaly import AlertThresholds, deduplicate, detect
from ..domain.enums import AgentName, AlertSeverity
from ..domain.kpi import (
    PerformanceSnapshot,
    health_score,
    health_status,
    summarize,
)
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)


class MonitorAgent(BaseAgent):
    """Turns raw delivery data into alerts and a portfolio health score."""

    name = AgentName.MONITOR

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        snapshots = self._resolve_snapshots(state, context)

        thresholds = AlertThresholds(
            ctr_floor=context.optimization.alert_ctr_floor,
            cpa_ceiling=context.optimization.alert_cpa_ceiling,
            roas_floor=context.optimization.alert_roas_floor,
            min_impressions=context.optimization.min_impressions_for_alerts,
        )
        alerts = deduplicate(
            detect(
                snapshots,
                thresholds,
                daily_budgets=context.daily_budgets,
            )
        )

        portfolio = summarize(snapshots)
        score = health_score(portfolio)
        critical = [a for a in alerts if a.severity == AlertSeverity.CRITICAL]

        health = {
            "status": health_status(score),
            "score": round(score, 2),
            "portfolio": portfolio.model_dump(),
            "alert_count": len(alerts),
            "critical_count": len(critical),
            "window_days": int(state.get("window_days", 7) or 7),
            "campaigns_monitored": len(snapshots),
        }

        message = self._build_message(portfolio.model_dump(), alerts, score, iteration)

        logger.info(
            "monitor_completed",
            run_id=context.run_id,
            campaigns=len(snapshots),
            alerts=len(alerts),
            health=round(score, 2),
        )

        return {
            "metrics": [snapshot.model_dump() for snapshot in snapshots],
            "alerts": [alert.to_dict() for alert in alerts],
            "health": health,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "campaigns": len(snapshots),
                "alerts": len(alerts),
                "health_score": round(score, 2),
            },
        }

    def _resolve_snapshots(
        self, state: AgentState, context: AgentContext
    ) -> list[PerformanceSnapshot]:
        """Prefer freshly fetched snapshots; fall back to state carried over."""
        if context.snapshots:
            return list(context.snapshots)

        restored: list[PerformanceSnapshot] = []
        for raw in state.get("metrics") or []:
            if isinstance(raw, PerformanceSnapshot):
                restored.append(raw)
            elif isinstance(raw, dict):
                try:
                    restored.append(PerformanceSnapshot.model_validate(raw))
                except Exception as exc:
                    logger.warning("monitor_skipped_invalid_metric", error=str(exc))
        return restored

    def _build_message(
        self,
        portfolio: dict[str, Any],
        alerts: list[Any],
        score: float,
        iteration: int,
    ) -> dict[str, Any]:
        status = health_status(score)
        parts = [
            "Monitored " + str(portfolio.get("campaigns", 0)) + " campaigns.",
            "Health " + str(round(score, 1)) + "/100 (" + status + ").",
            "CTR " + format(float(portfolio.get("ctr", 0.0)), ".3%") + ",",
            "ROAS " + format(float(portfolio.get("roas", 0.0)), ".2f") + ".",
        ]
        if alerts:
            worst = alerts[0]
            parts.append(
                "Raised "
                + str(len(alerts))
                + " alert(s); highest severity "
                + worst.severity.value
                + " on campaign "
                + worst.campaign_id
                + "."
            )
        else:
            parts.append("No anomalies detected.")
        return self._message(" ".join(parts), iteration=iteration)
