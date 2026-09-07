"""Optimize Agent - turns every upstream finding into concrete, gated actions.

Nothing here executes. The agent only proposes; execution happens in the action
service after approval (or immediately when approval is disabled), so a model
hallucination can never change spend on its own.
"""

from __future__ import annotations

from typing import Any

from ..core.ids import new_id
from ..core.logging import get_logger
from ..domain.budget import allocate, total_delta
from ..domain.enums import ActionStatus, ActionType, AgentName, AlertRule, AlertSeverity
from ..domain.kpi import PerformanceSnapshot
from ..domain.scoring import should_pause
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

# Rules whose remedy is to stop spending rather than to tune it.
PAUSE_RULES = frozenset({AlertRule.HIGH_CPA, AlertRule.BURN_RATE})
REFRESH_RULES = frozenset({AlertRule.LOW_CTR, AlertRule.FREQUENCY_FATIGUE})


class OptimizeAgent(BaseAgent):
    """Proposes budget, bid, creative and experiment actions."""

    name = AgentName.OPTIMIZE

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0) + 1
        snapshots = self._snapshots(state)
        alerts = list(state.get("alerts") or [])
        decisions = list(state.get("bidding_decisions") or [])
        new_creatives = list(state.get("new_creatives") or [])

        actions: list[dict[str, Any]] = []
        actions.extend(self._creative_actions(snapshots, context, iteration))
        allocations = allocate(
            snapshots,
            max_change_pct=context.optimization.max_budget_change_pct / 100.0,
            cross_check_with_solver=context.optimization.use_convex_solver,
        )
        actions.extend(self._budget_actions(allocations, iteration))
        actions.extend(self._bid_actions(decisions, iteration))
        actions.extend(self._alert_actions(alerts, iteration))
        actions.extend(self._experiment_actions(new_creatives, context, iteration))

        actions = self._dedupe(actions)
        is_complete = self._should_complete(iteration, state, alerts, actions)

        message = self._build_message(actions, allocations, iteration)
        logger.info(
            "optimize_completed",
            run_id=context.run_id,
            iteration=iteration,
            actions=len(actions),
            complete=is_complete,
        )

        return {
            "optimization_actions": actions,
            "budget_allocations": [
                {**allocation.model_dump(), "iteration": iteration} for allocation in allocations
            ],
            "iteration": iteration,
            "is_complete": is_complete,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "iteration": iteration,
                "actions": len(actions),
                "budget": total_delta(allocations),
                "is_complete": is_complete,
            },
        }

    def _snapshots(self, state: AgentState) -> list[PerformanceSnapshot]:
        restored: list[PerformanceSnapshot] = []
        for metric in state.get("metrics") or []:
            if isinstance(metric, PerformanceSnapshot):
                restored.append(metric)
            elif isinstance(metric, dict):
                try:
                    restored.append(PerformanceSnapshot.model_validate(metric))
                except Exception as exc:
                    logger.warning("optimize_skipped_invalid_metric", error=str(exc))
        return restored

    def _action(
        self,
        *,
        action_type: ActionType,
        campaign_id: str,
        reason: str,
        confidence: float,
        iteration: int,
        before_value: str = "",
        after_value: str = "",
        creative_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": new_id("act"),
            "run_id": None,
            "campaign_id": campaign_id,
            "creative_id": creative_id,
            "action_type": action_type.value,
            "status": ActionStatus.PROPOSED.value,
            "before_value": before_value,
            "after_value": after_value,
            "reason": reason,
            "confidence": round(max(0.0, min(confidence, 1.0)), 3),
            "proposed_by": self.name.value,
            "iteration": iteration,
        }

    def _creative_actions(
        self,
        snapshots: list[PerformanceSnapshot],
        context: AgentContext,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Pause creatives that are proven bad, never ones that are merely new."""
        actions: list[dict[str, Any]] = []
        threshold = context.optimization.creative_score_threshold

        for campaign_id, creatives in context.existing_creatives.items():
            for creative in creatives:
                if creative.get("status") != "active":
                    continue
                pause, reason = should_pause(
                    impressions=int(creative.get("impressions", 0) or 0),
                    clicks=int(creative.get("clicks", 0) or 0),
                    conversions=int(creative.get("conversions", 0) or 0),
                    cost=float(creative.get("cost", 0.0) or 0.0),
                    revenue=float(creative.get("revenue", 0.0) or 0.0),
                    threshold=threshold,
                )
                if not pause:
                    continue
                actions.append(
                    self._action(
                        action_type=ActionType.PAUSE_CREATIVE,
                        campaign_id=campaign_id,
                        creative_id=str(creative.get("creative_id", "")),
                        before_value="active",
                        after_value="paused",
                        reason=reason,
                        confidence=0.8,
                        iteration=iteration,
                    )
                )

        if not actions:
            for snapshot in snapshots:
                pause, reason = should_pause(
                    impressions=snapshot.impressions,
                    clicks=snapshot.clicks,
                    conversions=snapshot.conversions,
                    cost=snapshot.total_cost,
                    revenue=snapshot.total_revenue,
                    threshold=threshold,
                )
                if not pause:
                    continue
                actions.append(
                    self._action(
                        action_type=ActionType.PAUSE_CREATIVE,
                        campaign_id=snapshot.campaign_id,
                        before_value="active",
                        after_value="paused",
                        reason="Campaign-level proxy (no creative breakdown): " + reason,
                        confidence=0.45,
                        iteration=iteration,
                    )
                )
        return actions

    def _budget_actions(self, allocations: list[Any], iteration: int) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for allocation in allocations:
            if abs(allocation.change_pct) <= 5.0:
                continue
            actions.append(
                self._action(
                    action_type=ActionType.ADJUST_BUDGET,
                    campaign_id=allocation.campaign_id,
                    before_value=format(allocation.current_budget, ".2f"),
                    after_value=format(allocation.recommended_budget, ".2f"),
                    reason=allocation.reason,
                    confidence=0.75,
                    iteration=iteration,
                )
            )
        return actions

    def _bid_actions(self, decisions: list[dict[str, Any]], iteration: int) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for decision in decisions:
            multiplier = float(decision.get("multiplier", 1.0) or 1.0)
            if abs(multiplier - 1.0) < 0.05:
                continue
            direction = "raise" if multiplier > 1.0 else "lower"
            actions.append(
                self._action(
                    action_type=ActionType.ADJUST_BID,
                    campaign_id=str(decision.get("campaign_id", "")),
                    before_value="",
                    after_value=format(float(decision.get("bid_cpm", 0.0)), ".4f"),
                    reason=(
                        "Recommended to "
                        + direction
                        + " the bid by "
                        + format(abs(multiplier - 1.0) * 100.0, ".0f")
                        + "%. "
                        + str(decision.get("reasoning", ""))
                    ),
                    confidence=float(decision.get("confidence", 0.5) or 0.5),
                    iteration=iteration,
                )
            )
        return actions

    def _alert_actions(self, alerts: list[dict[str, Any]], iteration: int) -> list[dict[str, Any]]:
        """Map each alert rule onto its remedy instead of parsing message text."""
        actions: list[dict[str, Any]] = []
        for alert in alerts:
            try:
                rule = AlertRule(str(alert.get("rule", "")))
            except ValueError:
                logger.warning("optimize_unknown_alert_rule", rule=alert.get("rule"))
                continue

            campaign_id = str(alert.get("campaign_id", ""))
            severity = str(alert.get("severity", AlertSeverity.WARNING.value))
            confidence = 0.9 if severity == AlertSeverity.CRITICAL.value else 0.7
            message = str(alert.get("message", ""))

            if rule in PAUSE_RULES:
                actions.append(
                    self._action(
                        action_type=ActionType.PAUSE_CAMPAIGN,
                        campaign_id=campaign_id,
                        before_value="active",
                        after_value="paused",
                        reason="Alert " + rule.value + ": " + message,
                        confidence=confidence,
                        iteration=iteration,
                    )
                )
            elif rule in REFRESH_RULES:
                actions.append(
                    self._action(
                        action_type=ActionType.REFRESH_CREATIVE,
                        campaign_id=campaign_id,
                        reason="Alert " + rule.value + ": " + message,
                        confidence=confidence,
                        iteration=iteration,
                    )
                )
            elif rule == AlertRule.LOW_ROAS:
                actions.append(
                    self._action(
                        action_type=ActionType.ADJUST_BID,
                        campaign_id=campaign_id,
                        reason="Alert "
                        + rule.value
                        + ": lowering bids to protect margin. "
                        + message,
                        confidence=confidence,
                        iteration=iteration,
                    )
                )
            else:
                logger.debug("optimize_alert_without_remedy", rule=rule.value)
        return actions

    def _experiment_actions(
        self,
        new_creatives: list[dict[str, Any]],
        context: AgentContext,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Open an A/B test only when there is a control and enough variants."""
        by_campaign: dict[str, list[dict[str, Any]]] = {}
        for creative in new_creatives:
            by_campaign.setdefault(str(creative.get("campaign_id", "")), []).append(creative)

        actions: list[dict[str, Any]] = []
        for campaign_id, variants in by_campaign.items():
            if len(variants) < 2:
                continue
            existing = context.existing_creatives.get(campaign_id, [])
            control = next((c for c in existing if c.get("ab_group") == "control"), None)
            reason = (
                "Test "
                + str(len(variants))
                + " new variants against "
                + ("the existing control" if control else "each other")
            )
            actions.append(
                self._action(
                    action_type=ActionType.START_AB_TEST,
                    campaign_id=campaign_id,
                    reason=reason,
                    confidence=0.85 if control else 0.6,
                    iteration=iteration,
                )
            )
        return actions

    @staticmethod
    def _dedupe(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the highest-confidence action per (campaign, creative, type)."""
        best: dict[tuple[str, str, str], dict[str, Any]] = {}
        for action in actions:
            key = (
                str(action["campaign_id"]),
                str(action.get("creative_id") or ""),
                str(action["action_type"]),
            )
            current = best.get(key)
            if current is None or float(action["confidence"]) > float(current["confidence"]):
                best[key] = action
        return sorted(best.values(), key=lambda a: (-float(a["confidence"]), a["campaign_id"]))

    def _should_complete(
        self,
        iteration: int,
        state: AgentState,
        alerts: list[dict[str, Any]],
        actions: list[dict[str, Any]],
    ) -> bool:
        """Stop when the cap is hit, anomalies are cleared, or nothing is left to do."""
        max_iterations = int(state.get("max_iterations", 3) or 3)
        if iteration >= max_iterations:
            return True
        if not alerts:
            return True
        return not actions

    def _build_message(
        self, actions: list[dict[str, Any]], allocations: list[Any], iteration: int
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for action in actions:
            key = str(action["action_type"])
            counts[key] = counts.get(key, 0) + 1

        breakdown = (
            ", ".join(
                str(count) + " " + action_type for action_type, count in sorted(counts.items())
            )
            or "none"
        )
        delta = total_delta(allocations)

        return self._message(
            "Iteration "
            + str(iteration)
            + " proposed "
            + str(len(actions))
            + " action(s): "
            + breakdown
            + ". Budget plan moves "
            + format(delta["net_delta"], "+.2f")
            + " across "
            + str(delta["campaigns"])
            + " campaign(s).",
            iteration=iteration,
            extra={"action_counts": counts, "budget_delta": delta},
        )
