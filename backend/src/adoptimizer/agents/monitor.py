"""Monitor Agent - telemetry collection, anomaly detection and health scoring.

Detection runs on the warehouse, which is only ever a mirror of the networks.
So after raising alerts the agent asks each affected network what it thinks,
through the tool layer. That single read-only call is the difference between
"the warehouse says this campaign is burning cash" and "the network agrees",
and it is the reason a lagging ETL job does not become a paused campaign.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..core.clock import utc_today
from ..core.logging import get_logger
from ..domain.anomaly import Alert, AlertThresholds, deduplicate, detect
from ..domain.enums import AgentName, AlertRule, AlertSeverity
from ..domain.kpi import (
    PerformanceSnapshot,
    health_score,
    health_status,
    summarize,
)
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

# How many campaigns to reconcile against their network in one iteration. Each
# check is a real external call, so the bound lives here rather than being left
# to the global per-run tool budget alone: a portfolio with forty critical
# alerts should not turn one monitoring step into forty API requests.
MAX_LIVE_CHECKS = 5

# Worst first. Reconciling the alerts that can pause a campaign matters more
# than reconciling the ones that only suggest a creative refresh.
SEVERITY_RANK = {
    AlertSeverity.CRITICAL.value: 0,
    AlertSeverity.WARNING.value: 1,
    AlertSeverity.INFO.value: 2,
}


class MonitorAgent(BaseAgent):
    """Turns raw delivery data into alerts and a portfolio health score."""

    name = AgentName.MONITOR

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        window_days = int(state.get("window_days", 7) or 7)
        snapshots = self._resolve_snapshots(state, context)

        thresholds = AlertThresholds(
            ctr_floor=context.optimization.alert_ctr_floor,
            cpa_ceiling=context.optimization.alert_cpa_ceiling,
            roas_floor=context.optimization.alert_roas_floor,
            min_impressions=context.optimization.min_impressions_for_alerts,
            burn_rate_multiplier=context.optimization.burn_rate_multiplier,
            burn_rate_critical_multiplier=context.optimization.burn_rate_critical_multiplier,
            burn_rate_hysteresis=context.optimization.burn_rate_hysteresis,
        )
        alerts = deduplicate(
            detect(
                snapshots,
                thresholds,
                daily_budgets=context.daily_budgets,
                window_days=window_days,
                prior_severities=self._prior_burn_rate(state),
            )
        )

        portfolio = summarize(snapshots)
        score = health_score(portfolio)
        critical = [a for a in alerts if a.severity == AlertSeverity.CRITICAL]

        checks = await self._cross_check(alerts, context, window_days, iteration)

        health = {
            "status": health_status(score),
            "score": round(score, 2),
            "portfolio": portfolio.model_dump(),
            "alert_count": len(alerts),
            "critical_count": len(critical),
            "window_days": window_days,
            "campaigns_monitored": len(snapshots),
            "live_checks": len(checks),
            "live_checks_unresolved": sum(1 for c in checks if c["outcome"] == "unresolved"),
        }

        message = self._build_message(portfolio.model_dump(), alerts, score, iteration, checks)

        logger.info(
            "monitor_completed",
            run_id=context.run_id,
            campaigns=len(snapshots),
            alerts=len(alerts),
            health=round(score, 2),
            live_checks=len(checks),
        )

        return {
            "metrics": [snapshot.model_dump() for snapshot in snapshots],
            "alerts": [alert.to_dict() for alert in alerts],
            "platform_checks": checks,
            "health": health,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "campaigns": len(snapshots),
                "alerts": len(alerts),
                "health_score": round(score, 2),
                "live_checks": len(checks),
            },
        }

    async def _cross_check(
        self,
        alerts: list[Alert],
        context: AgentContext,
        window_days: int,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Ask each alerted campaign's own network whether it agrees with us.

        Returns an empty list when the tool layer is off, so monitoring still
        works in a deployment that has not enabled tools. An `unresolved` entry
        is not a failure to report: it records that the campaign cannot be
        reconciled because it carries no external id, which is itself something
        an operator needs to see before trusting the alert.
        """
        if context.tools is None or not context.tools.enabled or not alerts:
            return []

        end = utc_today()
        start = end - timedelta(days=max(1, window_days) - 1)
        worst = sorted(alerts, key=lambda item: SEVERITY_RANK.get(item.severity.value, 9))

        checks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for alert in worst:
            if len(checks) >= MAX_LIVE_CHECKS:
                break
            campaign_id = str(alert.campaign_id)
            if campaign_id in seen:
                continue
            seen.add(campaign_id)

            target = context.tool_target(campaign_id)
            if target is None:
                checks.append(
                    {
                        "campaign_id": campaign_id,
                        "rule": alert.rule.value,
                        "severity": alert.severity.value,
                        "outcome": "unresolved",
                        "error": "no platform external_id on record; cannot reconcile",
                    }
                )
                continue

            platform, external_id = target
            result = await context.call_tool(
                "platform.campaign_report",
                {
                    "platform": platform,
                    "campaign_external_id": external_id,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                },
                agent=self.name,
                idempotency_key="monitor-report:" + str(iteration) + ":" + campaign_id,
            )
            if result is None:
                continue

            data = result.data if isinstance(result.data, dict) else {}
            rows = data.get("rows")
            checks.append(
                {
                    "campaign_id": campaign_id,
                    "platform": platform,
                    "external_id": external_id,
                    "rule": alert.rule.value,
                    "severity": alert.severity.value,
                    "tool": result.request.tool,
                    "outcome": result.outcome.value,
                    "served_by": str(data.get("served_by") or ""),
                    "live_rows": len(rows) if isinstance(rows, list) else 0,
                    "window": start.isoformat() + "/" + end.isoformat(),
                    "duration_ms": round(result.duration_ms, 2),
                    "error": result.error,
                }
            )
        return checks

    @staticmethod
    def _prior_burn_rate(state: AgentState) -> dict[str, str]:
        """Last pass's burn-rate severity per campaign, for the hysteresis band.

        The monitor runs first in each iteration, so the alerts channel still
        holds the previous iteration's verdicts at this point. Only burn rate is
        carried over: it is the rule whose critical line gates whether the
        reconciliation step may overrule the anomaly, and a classification that
        flips on a rounding difference is worse than no classification at all.
        """
        prior: dict[str, str] = {}
        # Typed loosely on purpose: a replayed run restores this channel from
        # JSON, where nothing enforces the shape the schema declares.
        raw: list[Any] = list(state.get("alerts") or [])
        for alert in raw:
            if not isinstance(alert, dict):
                continue
            if str(alert.get("rule") or "") != AlertRule.BURN_RATE.value:
                continue
            prior[str(alert.get("campaign_id") or "")] = str(alert.get("severity") or "")
        return prior

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
        checks: list[dict[str, Any]] | None = None,
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
        if checks:
            answered = sum(1 for item in checks if item.get("served_by"))
            blind = sum(1 for item in checks if item.get("outcome") == "unresolved")
            parts.append(
                "Cross-checked "
                + str(len(checks))
                + " alerted campaign(s) against their network: "
                + str(answered)
                + " answered, "
                + str(blind)
                + " without an external id."
            )
        return self._message(" ".join(parts), iteration=iteration)
