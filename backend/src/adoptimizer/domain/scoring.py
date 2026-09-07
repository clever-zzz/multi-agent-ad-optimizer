"""Creative and campaign performance scoring.

Scores are decomposed into named components so a rejection can always be
explained to an operator, and the volume gate prevents pausing a creative that
simply has not been shown enough yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from .statistics import clamp, safe_ratio, wilson_interval

CTR_REFERENCE = 0.05
CVR_REFERENCE = 0.10
CPA_REFERENCE = 200.0
ROAS_REFERENCE = 3.0

WEIGHT_CTR = 0.30
WEIGHT_CVR = 0.25
WEIGHT_CPA = 0.20
WEIGHT_ROAS = 0.25

DEFAULT_MIN_IMPRESSIONS = 500


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Weighted components behind a composite score."""

    ctr_component: float
    cvr_component: float
    cpa_component: float
    roas_component: float
    confidence: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "ctr_component": round(self.ctr_component, 3),
            "cvr_component": round(self.cvr_component, 3),
            "cpa_component": round(self.cpa_component, 3),
            "roas_component": round(self.roas_component, 3),
            "confidence": round(self.confidence, 3),
            "total": round(self.total, 2),
        }


def score_performance(
    *,
    impressions: int,
    clicks: int,
    conversions: int,
    cost: float,
    revenue: float = 0.0,
) -> ScoreBreakdown:
    """Compute a 0-100 composite score with an explicit confidence factor."""
    ctr = safe_ratio(clicks, impressions)
    cvr = safe_ratio(conversions, clicks)
    cpa = safe_ratio(cost, conversions) if conversions > 0 else None
    roas = safe_ratio(revenue, cost)

    ctr_component = clamp(safe_ratio(ctr, CTR_REFERENCE), 0.0, 1.0) * WEIGHT_CTR * 100.0
    cvr_component = clamp(safe_ratio(cvr, CVR_REFERENCE), 0.0, 1.0) * WEIGHT_CVR * 100.0

    if cpa is None:
        # No conversions yet: neutral rather than zero, the confidence factor
        # below keeps an unproven creative from being auto-paused.
        cpa_component = 0.5 * WEIGHT_CPA * 100.0
    else:
        cpa_component = clamp(1.0 - safe_ratio(cpa, CPA_REFERENCE), 0.0, 1.0) * WEIGHT_CPA * 100.0

    roas_component = clamp(safe_ratio(roas, ROAS_REFERENCE), 0.0, 1.0) * WEIGHT_ROAS * 100.0

    confidence = clamp(
        0.25
        + 0.5 * min(safe_ratio(impressions, 20_000.0), 1.0)
        + 0.25 * min(safe_ratio(conversions, 20.0), 1.0),
        0.0,
        1.0,
    )

    raw = ctr_component + cvr_component + cpa_component + roas_component
    # Blend toward the neutral midpoint when evidence is thin.
    total = raw * confidence + 50.0 * (1.0 - confidence)

    return ScoreBreakdown(
        ctr_component=ctr_component,
        cvr_component=cvr_component,
        cpa_component=cpa_component,
        roas_component=roas_component,
        confidence=confidence,
        total=total,
    )


def should_pause(
    *,
    impressions: int,
    clicks: int,
    conversions: int,
    cost: float,
    revenue: float = 0.0,
    threshold: float = 40.0,
    min_impressions: int = DEFAULT_MIN_IMPRESSIONS,
    min_confidence: float = 0.6,
) -> tuple[bool, str]:
    """Decide whether a creative is proven bad enough to pause.

    Requires both a low score and enough evidence, so pausing is never driven
    by a handful of impressions.
    """
    if impressions < min_impressions:
        return False, "Below the " + str(min_impressions) + " impression evidence gate"

    breakdown = score_performance(
        impressions=impressions, clicks=clicks, conversions=conversions, cost=cost, revenue=revenue
    )
    if breakdown.confidence < min_confidence:
        return False, "Confidence " + format(
            breakdown.confidence, ".2f"
        ) + " is below the " + format(min_confidence, ".2f") + " gate"

    if breakdown.total >= threshold:
        return False, "Score " + format(breakdown.total, ".1f") + " is at or above the " + format(
            threshold, ".1f"
        ) + " threshold"

    return True, "Score " + format(breakdown.total, ".1f") + " is below the " + format(
        threshold, ".1f"
    ) + " threshold"


def rank_by_lower_bound(
    items: list[tuple[str, int, int]],
) -> list[tuple[str, float]]:
    """Rank (id, successes, trials) pairs by Wilson lower bound.

    Ranking by raw rate would put a 1-for-1 creative above a 300-for-10000 one.
    """
    ranked = [(key, wilson_interval(successes, trials).lower) for key, successes, trials in items]
    ranked.sort(key=lambda pair: (-pair[1], pair[0]))
    return ranked
