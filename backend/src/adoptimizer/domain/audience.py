"""Audience segmentation and expansion logic.

Segment scoring is driven by observed conversion share versus delivery share,
which is what actually identifies an over- or under-performing audience. The
demo returned a hard-coded list of four segments regardless of input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .statistics import clamp, safe_ratio, wilson_interval

DEFAULT_LOOKALIKE_TIERS = (1.0, 2.0, 5.0)


@dataclass(frozen=True, slots=True)
class SegmentObservation:
    """Delivery and conversion counts for one audience slice."""

    key: str
    dimension: str
    impressions: int
    clicks: int
    conversions: int
    cost: float
    revenue: float


@dataclass(frozen=True, slots=True)
class SegmentInsight:
    """A scored, explainable audience segment."""

    key: str
    dimension: str
    impressions: int
    clicks: int
    conversions: int
    cost: float
    revenue: float
    ctr: float
    cvr: float
    cvr_lower_bound: float
    cpa: float | None
    roas: float
    delivery_share: float
    conversion_share: float
    index: float
    score: float
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "dimension": self.dimension,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "conversions": self.conversions,
            "cost": round(self.cost, 2),
            "revenue": round(self.revenue, 2),
            "ctr": round(self.ctr, 6),
            "cvr": round(self.cvr, 6),
            "cvr_lower_bound": round(self.cvr_lower_bound, 6),
            "cpa": None if self.cpa is None else round(self.cpa, 2),
            "roas": round(self.roas, 4),
            "delivery_share": round(self.delivery_share, 4),
            "conversion_share": round(self.conversion_share, 4),
            "index": round(self.index, 3),
            "score": round(self.score, 2),
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True, slots=True)
class AudienceAnalysis:
    """Full output of the audience agent."""

    segments: list[SegmentInsight] = field(default_factory=list)
    dimension_summaries: dict[str, int] = field(default_factory=dict)
    lookalike_suggestions: list[dict[str, Any]] = field(default_factory=list)
    total_impressions: int = 0
    total_conversions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "dimension_summaries": dict(self.dimension_summaries),
            "lookalike_suggestions": list(self.lookalike_suggestions),
            "total_impressions": self.total_impressions,
            "total_conversions": self.total_conversions,
        }

    @property
    def top_segments(self) -> list[SegmentInsight]:
        return [s for s in self.segments if s.score >= 60.0]


def _recommend(index: float, conversions: int, cvr_lower: float) -> str:
    if conversions == 0:
        return "No conversions recorded; gather more data before scaling"
    if index >= 1.3:
        return "Converts well above its delivery share; increase bid and budget"
    if index >= 1.05:
        return "Slightly outperforming; a good lookalike seed"
    if index >= 0.8:
        return "Performing in line with the portfolio; hold targeting"
    if cvr_lower > 0:
        return "Statistically underperforming; narrow or exclude this slice"
    return "Underperforming but not yet significant; keep monitoring"


def analyze(
    observations: list[SegmentObservation],
    *,
    top_n: int = 20,
    min_impressions: int = 100,
) -> AudienceAnalysis:
    """Score every observed segment against portfolio averages."""
    if not observations:
        return AudienceAnalysis()

    total_impressions = sum(o.impressions for o in observations)
    total_conversions = sum(o.conversions for o in observations)

    insights: list[SegmentInsight] = []
    for observation in observations:
        if observation.impressions < min_impressions:
            continue

        ctr = safe_ratio(observation.clicks, observation.impressions)
        cvr = safe_ratio(observation.conversions, observation.clicks)
        cvr_lower = wilson_interval(observation.conversions, observation.clicks).lower
        cpa = observation.cost / observation.conversions if observation.conversions else None
        roas = safe_ratio(observation.revenue, observation.cost)

        delivery_share = safe_ratio(observation.impressions, total_impressions)
        conversion_share = safe_ratio(observation.conversions, total_conversions)
        index = safe_ratio(conversion_share, delivery_share) if delivery_share > 0 else 0.0

        score = clamp(
            50.0 * clamp(safe_ratio(index, 1.5), 0.0, 1.0)
            + 30.0 * clamp(safe_ratio(cvr, 0.10), 0.0, 1.0)
            + 20.0 * clamp(safe_ratio(roas, 3.0), 0.0, 1.0),
            0.0,
            100.0,
        )

        insights.append(
            SegmentInsight(
                key=observation.key,
                dimension=observation.dimension,
                impressions=observation.impressions,
                clicks=observation.clicks,
                conversions=observation.conversions,
                cost=observation.cost,
                revenue=observation.revenue,
                ctr=ctr,
                cvr=cvr,
                cvr_lower_bound=cvr_lower,
                cpa=cpa,
                roas=roas,
                delivery_share=delivery_share,
                conversion_share=conversion_share,
                index=index,
                score=score,
                recommendation=_recommend(index, observation.conversions, cvr_lower),
            )
        )

    insights.sort(key=lambda s: (-s.score, -s.conversions, s.key))

    dimension_summaries: dict[str, int] = {}
    for insight in insights:
        dimension_summaries[insight.dimension] = dimension_summaries.get(insight.dimension, 0) + 1

    return AudienceAnalysis(
        segments=insights[:top_n],
        dimension_summaries=dimension_summaries,
        lookalike_suggestions=_suggest_lookalikes(insights),
        total_impressions=total_impressions,
        total_conversions=total_conversions,
    )


def _suggest_lookalikes(
    insights: list[SegmentInsight], tiers: tuple[float, ...] = DEFAULT_LOOKALIKE_TIERS
) -> list[dict[str, Any]]:
    """Propose expansion tiers seeded from the strongest segments."""
    seeds = [s for s in insights if s.index >= 1.1 and s.conversions >= 5][:3]
    suggestions: list[dict[str, Any]] = []
    for seed in seeds:
        for tier in tiers:
            suggestions.append(
                {
                    "seed_segment": seed.key,
                    "dimension": seed.dimension,
                    "expansion_pct": tier,
                    "estimated_reach": int(seed.impressions * (1.0 + tier * 8.0)),
                    "expected_index": round(seed.index * (1.0 - 0.06 * tier), 3),
                    "rationale": (
                        "Seed converts at "
                        + format(seed.index, ".2f")
                        + "x its delivery share with "
                        + str(seed.conversions)
                        + " conversions"
                    ),
                }
            )
    return suggestions
