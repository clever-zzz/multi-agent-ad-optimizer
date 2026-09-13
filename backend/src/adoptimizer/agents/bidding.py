"""Bidding Agent - per-campaign bid recommendation.

All pricing maths lives in domain.pricing so this agent is only responsible for
selecting targets, applying configuration and explaining the result. The demo
version applied the ROAS multiplier and then divided by the same multiplier,
which cancelled the adjustment out entirely; that path is gone.
"""

from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from ..domain.enums import AgentName
from ..domain.kpi import PerformanceSnapshot
from ..domain.pricing import recommend_bid
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)


class BiddingAgent(BaseAgent):
    """Recommends a capped bid per campaign with an auditable rationale."""

    name = AgentName.BIDDING

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        snapshots = self._snapshots(state)

        decisions: list[dict[str, Any]] = []
        for snapshot in snapshots:
            targets = context.campaign_targets.get(snapshot.campaign_id, {})
            target_roas = float(
                targets.get("target_roas", context.optimization.default_target_roas)
            )
            recommendation = recommend_bid(
                campaign_id=snapshot.campaign_id,
                predicted_ctr=snapshot.predicted_ctr,
                predicted_cvr=snapshot.predicted_cvr,
                observed_roas=snapshot.roas,
                impressions=snapshot.impressions,
                target_cpa=float(
                    targets.get("target_cpa", context.optimization.default_target_cpa)
                ),
                target_roas=target_roas,
                bid_cap_ratio=context.optimization.bid_cap_ratio_of_target_cpa,
            )
            decisions.append(
                {
                    "campaign_id": recommendation.campaign_id,
                    "campaign_name": snapshot.campaign_name,
                    "bid_cpm": recommendation.bid_cpm,
                    "max_cpc": recommendation.max_cpc,
                    "ecpm": recommendation.ecpm,
                    "predicted_ctr": recommendation.predicted_ctr,
                    "predicted_cvr": recommendation.predicted_cvr,
                    "multiplier": recommendation.multiplier,
                    "confidence": recommendation.confidence,
                    "reasoning": recommendation.reasoning,
                    "iteration": iteration,
                    # The frame this recommendation was judged against. Carried
                    # on the decision so the proposal can record it, which is
                    # what lets the critic see that a bid raise and a budget cut
                    # were decided against *different* references.
                    "observed_roas": round(float(snapshot.roas), 4),
                    "target_roas": target_roas,
                }
            )

        message = self._build_message(decisions, iteration)
        logger.info("bidding_completed", run_id=context.run_id, decisions=len(decisions))

        return {
            "bidding_decisions": decisions,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "decisions": len(decisions),
                "avg_bid_cpm": round(sum(d["bid_cpm"] for d in decisions) / len(decisions), 4)
                if decisions
                else 0.0,
                "avg_confidence": round(sum(d["confidence"] for d in decisions) / len(decisions), 3)
                if decisions
                else 0.0,
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
                    logger.warning("bidding_skipped_invalid_metric", error=str(exc))
        return restored

    def _build_message(self, decisions: list[dict[str, Any]], iteration: int) -> dict[str, Any]:
        if not decisions:
            return self._message(
                "No campaigns were eligible for a bid update.", iteration=iteration
            )

        raised = sum(1 for d in decisions if d["multiplier"] > 1.0)
        lowered = sum(1 for d in decisions if d["multiplier"] < 1.0)
        held = len(decisions) - raised - lowered
        average_cpm = sum(d["bid_cpm"] for d in decisions) / len(decisions)

        return self._message(
            "Updated bids for "
            + str(len(decisions))
            + " campaign(s): "
            + str(raised)
            + " raised, "
            + str(lowered)
            + " lowered, "
            + str(held)
            + " held. Average CPM bid "
            + format(average_cpm, ".2f")
            + ".",
            iteration=iteration,
        )
