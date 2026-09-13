"""Optimize Agent - turns every upstream finding into concrete, gated actions.

Nothing here executes. The agent only proposes; execution happens in the action
service after approval (or immediately when approval is disabled), so a model
hallucination can never change spend on its own.

What it does do is rehearse. Every proposal that would mutate an ad account is
handed to the tool layer as a dry run before it reaches the approval queue, so
"the network would reject this" is discovered by the agent rather than by the
operator who clicked approve.
"""

from __future__ import annotations

from typing import Any

from ..core.ids import new_id
from ..core.logging import get_logger
from ..domain.budget import BudgetAllocation, allocate, total_delta
from ..domain.enums import ActionStatus, ActionType, AgentName, AlertRule, AlertSeverity
from ..domain.kpi import PerformanceSnapshot
from ..domain.scoring import should_pause
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

# Rules whose remedy is to stop spending rather than to tune it.
PAUSE_RULES = frozenset({AlertRule.HIGH_CPA, AlertRule.BURN_RATE})
REFRESH_RULES = frozenset({AlertRule.LOW_CTR, AlertRule.FREQUENCY_FATIGUE})

# Which tool carries each proposal that touches a campaign. Anything absent from
# these two maps is either advisory (refresh_creative, expand_audience,
# start_ab_test) or applied through a per-network bidding API with no single
# call shape (adjust_bid), so it is recorded locally and never preflighted.
CAMPAIGN_TOOLS = {
    ActionType.ADJUST_BUDGET: "platform.set_daily_budget",
    ActionType.PAUSE_CAMPAIGN: "platform.pause_campaign",
    ActionType.RESUME_CAMPAIGN: "platform.resume_campaign",
}
CREATIVE_TOOLS = {
    ActionType.PAUSE_CREATIVE: "platform.pause_creative",
    ActionType.RESUME_CREATIVE: "platform.resume_creative",
}

# Mirrors REASON_MAX in the tool schema. Truncating here rather than letting the
# executor refuse the call keeps a long rationale from masquerading as a
# malformed proposal.
REASON_MAX = 200

# Dry runs cost executor budget and audit rows even though they touch nothing.
# Capping them per iteration keeps a pathological run from spending its whole
# tool budget on rehearsal and leaving none for the monitor's live checks.
MAX_PREFLIGHTS_PER_ITERATION = 25

# How far a bid multiplier has to move before it counts as a proposal at all.
# Shared by _bid_actions and the budget guard below, so "this run wants to spend
# more here" means exactly "a raise-bid proposal is in the queue".
BID_MOVE_THRESHOLD = 0.05


def _budget_value(raw: Any) -> float | None:
    """Parse a proposed budget that may carry a currency symbol."""
    cleaned = str(raw or "").replace("\u00a5", "").replace("$", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


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
        held_at_target = self._hold_at_target(snapshots, decisions, context)
        allocations = allocate(
            snapshots,
            max_change_pct=context.optimization.max_budget_change_pct / 100.0,
            daily_budgets=context.daily_budgets,
            cross_check_with_solver=context.optimization.use_convex_solver,
            no_decrease=held_at_target,
        )
        self._log_budget_holds(held_at_target, allocations, context, iteration)
        actions.extend(self._budget_actions(allocations, iteration))
        actions.extend(self._bid_actions(decisions, iteration))
        actions.extend(self._alert_actions(alerts, iteration))
        actions.extend(self._experiment_actions(new_creatives, context, iteration))

        actions = self._dedupe(actions)
        preflights = await self._preflight(actions, context, iteration)
        is_complete = self._should_complete(iteration, state, alerts, actions)

        message = self._build_message(actions, allocations, iteration, preflights)
        logger.info(
            "optimize_completed",
            run_id=context.run_id,
            iteration=iteration,
            actions=len(actions),
            preflights=len(preflights),
            blocked=sum(1 for item in preflights if item["blocking"]),
            complete=is_complete,
        )

        return {
            "optimization_actions": actions,
            "tool_preflights": preflights,
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
                "preflights": len(preflights),
                "preflights_blocked": sum(1 for item in preflights if item["blocking"]),
                "is_complete": is_complete,
            },
        }

    async def _preflight(
        self,
        actions: list[dict[str, Any]],
        context: AgentContext,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Rehearse every write proposal and annotate it with the verdict.

        The annotation travels on the proposal itself, so the critic can refuse
        to pass along anything the platform would reject and the approval screen
        can show the operator why. Records are also returned separately because
        proposals accumulate across iterations while the run-level view of "what
        was rehearsed" should not.
        """
        if context.tools is None or not context.tools.enabled:
            return []

        records: list[dict[str, Any]] = []
        attempted = 0
        for action in actions:
            if attempted >= MAX_PREFLIGHTS_PER_ITERATION:
                break
            call = self._preflight_call(action, context)
            if call is None:
                continue
            attempted += 1
            tool, arguments, target_id = call
            result = await context.call_tool(
                tool,
                arguments,
                agent=self.name,
                idempotency_key=("preflight:" + str(iteration) + ":" + str(action.get("id") or "")),
            )
            if result is None:
                continue

            record = {
                "action_id": str(action.get("id") or ""),
                "action_type": str(action.get("action_type") or ""),
                "campaign_id": str(action.get("campaign_id") or ""),
                "target_id": target_id,
                "tool": tool,
                "iteration": iteration,
                "outcome": result.outcome.value,
                "dry_run": result.dry_run,
                "refused": result.refused,
                "blocking": result.blocking,
                "duration_ms": round(result.duration_ms, 2),
                "error": result.error,
            }
            records.append(record)
            action["preflight"] = {
                "tool": tool,
                "outcome": record["outcome"],
                "refused": record["refused"],
                "blocking": record["blocking"],
                "error": result.error,
            }
        return records

    def _preflight_call(
        self, action: dict[str, Any], context: AgentContext
    ) -> tuple[str, dict[str, Any], str] | None:
        """Build the tool call a proposal implies, or None when it implies none.

        Returning None is a deliberate non-verdict, not a pass: it means the
        proposal is advisory, or the campaign was never synced to a network, so
        there is nothing to rehearse. The critic treats those differently from a
        rehearsal that came back refused.
        """
        try:
            action_type = ActionType(str(action.get("action_type") or ""))
        except ValueError:
            return None

        campaign_id = str(action.get("campaign_id") or "")
        ref = context.campaign_ref(campaign_id)
        if ref is None:
            return None
        reason = str(action.get("reason") or "")[:REASON_MAX] or action_type.value

        if action_type in CAMPAIGN_TOOLS:
            target = context.tool_target(campaign_id)
            if target is None:
                return None
            platform, external_id = target
            arguments: dict[str, Any] = {
                "platform": platform,
                "campaign_external_id": external_id,
                "reason": reason,
            }
            if action_type == ActionType.ADJUST_BUDGET:
                budget = _budget_value(action.get("after_value"))
                if budget is None:
                    return None
                arguments["daily_budget"] = budget
            return CAMPAIGN_TOOLS[action_type], arguments, external_id

        if action_type in CREATIVE_TOOLS:
            creative_id = str(action.get("creative_id") or "")
            if not creative_id:
                # A campaign-level proxy proposal names no specific asset, so
                # there is nothing concrete for the network to accept or reject.
                return None
            platform = str(ref.get("platform") or "")
            if not platform:
                return None
            return (
                CREATIVE_TOOLS[action_type],
                {
                    "platform": platform,
                    "creative_external_id": creative_id,
                    "reason": reason,
                },
                creative_id,
            )
        return None

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
        severity: str = "",
        direction: str = "",
        basis: dict[str, Any] | None = None,
        urgency: float = 0.0,
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
            # Only alert-derived proposals carry this. It tells the critic that
            # a fired anomaly rule stands behind the proposal, which is a
            # different claim than "the delivery sample was large enough".
            "severity": severity,
            # How far past its threshold the anomaly is, continuous rather than
            # the three-valued severity. The critic uses it to give a *clearly*
            # critical anomaly absolute precedence without handing that power to
            # one that only just crossed the line.
            "urgency": round(float(urgency), 4),
            # Which way spend moves, and the reference frame that decided it.
            # Structured rather than left in the prose so the critic can tell an
            # opposing pair from a coherent one.
            "direction": direction,
            "basis": dict(basis or {}),
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

    def _hold_at_target(
        self,
        snapshots: list[PerformanceSnapshot],
        decisions: list[dict[str, Any]],
        context: AgentContext,
    ) -> set[str]:
        """Campaigns whose budget this run must not cut.

        Two conditions, and both are required. The campaign clears its own ROAS
        target - the same test the bid agent uses - *and* this run is actually
        proposing to raise its bid. The guard exists to stop one run from saying
        "bid more here" and "spend less here" about the same campaign, so it only
        needs to fire where that contradiction would arise.

        Guarding every campaign that merely clears its target is much wider, and
        it backfires. A real portfolio runs above target most of the time, so
        every lower bound gets pinned to its current budget, the allocator is
        left with no headroom, and the budget agent proposes nothing at all - a
        guard that deletes the very plan it was added to keep coherent.
        """
        raising = self._raising_bid_ids(decisions)
        protected: set[str] = set()
        for snapshot in snapshots:
            if snapshot.campaign_id not in raising:
                continue
            targets = context.campaign_targets.get(snapshot.campaign_id, {})
            target = float(targets.get("target_roas", context.optimization.default_target_roas))
            if target > 0 and snapshot.roas >= target:
                protected.add(snapshot.campaign_id)
        return protected

    @staticmethod
    def _raising_bid_ids(decisions: list[dict[str, Any]]) -> set[str]:
        """Campaigns carrying a raise-bid proposal in this run.

        The threshold matches _bid_actions, so a decision that is about to be
        dropped as noise cannot be the reason a budget is protected.
        """
        return {
            str(decision.get("campaign_id", ""))
            for decision in decisions
            if float(decision.get("multiplier", 1.0) or 1.0) - 1.0 >= BID_MOVE_THRESHOLD
        }

    def _log_budget_holds(
        self,
        held: set[str],
        allocations: list[BudgetAllocation],
        context: AgentContext,
        iteration: int,
    ) -> None:
        """Audit which budget holds survived the allocator.

        allocate() releases holds when keeping them would leave it no headroom,
        so a campaign named here can still be cut. That trade is intended, but
        an operator reading a cut on a campaign that clears its target deserves
        the line explaining why.
        """
        if not held:
            return
        released = sorted(
            allocation.campaign_id
            for allocation in allocations
            if allocation.campaign_id in held and allocation.delta < 0
        )
        logger.info(
            "optimize_budget_holds",
            run_id=context.run_id,
            iteration=iteration,
            held=len(held),
            kept=len(held) - len(released),
            released=len(released),
            released_campaign_ids=released,
        )

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
                    direction=(
                        "increase"
                        if allocation.recommended_budget > allocation.current_budget
                        else "decrease"
                    ),
                    # Judged against the *portfolio* average rather than the
                    # campaign's own target, which is why it can disagree with a
                    # bid proposal that used the target.
                    basis={
                        "metric": "roas",
                        "reference": "portfolio",
                        "change_pct": round(float(allocation.change_pct), 2),
                        "score": round(float(allocation.score), 4),
                        "solver": allocation.solver,
                    },
                )
            )
        return actions

    def _bid_actions(self, decisions: list[dict[str, Any]], iteration: int) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for decision in decisions:
            multiplier = float(decision.get("multiplier", 1.0) or 1.0)
            if abs(multiplier - 1.0) < BID_MOVE_THRESHOLD:
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
                    direction="increase" if multiplier > 1.0 else "decrease",
                    # The reference this was judged against is the campaign's own
                    # target, not the portfolio. Recording it is what lets the
                    # critic recognise a bid raise and a budget cut as answers to
                    # two different questions rather than a coherent plan.
                    basis={
                        "metric": "roas",
                        "reference": "target_roas",
                        "observed": decision.get("observed_roas"),
                        "reference_value": decision.get("target_roas"),
                        "magnitude": round(abs(multiplier - 1.0), 4),
                    },
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
                        severity=severity,
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
                        severity=severity,
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
                        severity=severity,
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
        self,
        actions: list[dict[str, Any]],
        allocations: list[Any],
        iteration: int,
        preflights: list[dict[str, Any]],
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
        rehearsed = len(preflights)
        blocked = sum(1 for item in preflights if item["blocking"])
        rehearsal = (
            " Rehearsed "
            + str(rehearsed)
            + " write proposal(s) against the tool layer; "
            + str(blocked)
            + " came back blocked."
            if rehearsed
            else ""
        )

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
            + " campaign(s)."
            + rehearsal,
            iteration=iteration,
            extra={
                "action_counts": counts,
                "budget_delta": delta,
                "preflights": {"attempted": rehearsed, "blocked": blocked},
            },
        )
