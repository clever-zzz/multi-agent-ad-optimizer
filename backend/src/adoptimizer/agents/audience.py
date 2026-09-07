"""Audience Agent - segmentation, scoring and lookalike expansion."""

from __future__ import annotations

import json
from typing import Any

from ..core.logging import get_logger
from ..domain.audience import SegmentObservation, analyze
from ..domain.enums import AgentName
from ..domain.kpi import PerformanceSnapshot
from ..llm.base import CompletionRequest
from ..orchestrator.state import AgentState
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are a senior performance-marketing audience strategist.
Given quantitative segment performance, state one testable hypothesis about why
the strongest segments convert, and name any slice that should be excluded.
Respond with JSON only: {"hypothesis": str, "recommended_exclusions": [str],
"confidence": float, "notes": str}."""


class AudienceAgent(BaseAgent):
    """Ranks audience slices by conversion index and proposes expansions."""

    name = AgentName.AUDIENCE

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        observations = self._resolve_observations(state, context)
        analysis = analyze(observations)

        insights: dict[str, Any] = analysis.to_dict()
        insights["observation_count"] = len(observations)

        narrative = await self._llm_hypothesis(analysis.to_dict(), context)
        if narrative:
            insights["hypothesis"] = narrative

        top = analysis.top_segments[:3]
        message = self._build_message(analysis.segments, top, iteration)

        logger.info(
            "audience_completed",
            run_id=context.run_id,
            segments=len(analysis.segments),
            top=len(top),
        )

        return {
            "audience_insights": insights,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "segments": len(analysis.segments),
                "top_segments": [s.key for s in top],
                "lookalike_suggestions": len(analysis.lookalike_suggestions),
            },
        }

    def _resolve_observations(
        self, state: AgentState, context: AgentContext
    ) -> list[SegmentObservation]:
        """Use warehouse demographics when present, else fall back to campaigns."""
        if context.audience_observations:
            return list(context.audience_observations)

        raw = state.get("audience_observations") or []
        if raw:
            return [
                o if isinstance(o, SegmentObservation) else SegmentObservation(**o) for o in raw
            ]

        observations: list[SegmentObservation] = []
        for metric in state.get("metrics") or []:
            snapshot = (
                metric
                if isinstance(metric, PerformanceSnapshot)
                else PerformanceSnapshot.model_validate(metric)
            )
            observations.append(
                SegmentObservation(
                    key=snapshot.campaign_name or snapshot.campaign_id,
                    dimension="campaign",
                    impressions=snapshot.impressions,
                    clicks=snapshot.clicks,
                    conversions=snapshot.conversions,
                    cost=snapshot.total_cost,
                    revenue=snapshot.total_revenue,
                )
            )
        return observations

    async def _llm_hypothesis(
        self, insights: dict[str, Any], context: AgentContext
    ) -> dict[str, Any] | None:
        """Ask the model for a qualitative read; never fail the run over it."""
        segments = insights.get("segments", [])[:8]
        if not segments:
            return None
        try:
            result = await context.gateway.complete(
                CompletionRequest(
                    task="audience_insight",
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=(
                        "Segment performance (conversion index > 1 means the slice converts "
                        "better than its delivery share):\n" + _format_segments(segments)
                    ),
                    schema_hint=(
                        '{"hypothesis": str, "recommended_exclusions": [str], '
                        '"confidence": float, "notes": str}'
                    ),
                ),
                run_id=context.run_id,
                agent=self.name.value,
            )
            parsed: dict[str, Any] = json.loads(result.text)
            return parsed
        except Exception as exc:
            logger.warning("audience_hypothesis_skipped", error=str(exc))
            return None

    def _build_message(self, segments: list[Any], top: list[Any], iteration: int) -> dict[str, Any]:
        if not segments:
            return self._message(
                "No audience segments had enough delivery to analyse.", iteration=iteration
            )
        names = ", ".join(s.key for s in top) if top else "none above the score gate"
        return self._message(
            "Analysed " + str(len(segments)) + " audience slices. Strongest: " + names + ".",
            iteration=iteration,
        )


def _format_segments(segments: list[dict[str, Any]]) -> str:
    lines = []
    for segment in segments:
        lines.append(
            "- "
            + str(segment.get("key"))
            + " ("
            + str(segment.get("dimension"))
            + ")"
            + " index="
            + format(float(segment.get("index", 0.0)), ".2f")
            + " cvr="
            + format(float(segment.get("cvr", 0.0)), ".3%")
            + " conversions="
            + str(segment.get("conversions", 0))
            + " roas="
            + format(float(segment.get("roas", 0.0)), ".2f")
        )
    return "\n".join(lines)
