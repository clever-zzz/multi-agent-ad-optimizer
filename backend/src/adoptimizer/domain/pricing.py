"""Bid and eCPM pricing mathematics.

Bid semantics are stated explicitly because conflating per-impression and
per-click prices is the single most common RTB implementation bug. The demo
computed a per-impression price, multiplied it by a ROAS multiplier and then
divided by the very same multiplier, so the adjustment silently cancelled out.
"""

from __future__ import annotations

from dataclasses import dataclass

from .statistics import clamp

DEFAULT_TARGET_CPA = 100.0
DEFAULT_TARGET_ROAS = 2.0
MIN_BID = 0.01
DEFAULT_BID_CAP_RATIO = 0.8


@dataclass(frozen=True, slots=True)
class BidRecommendation:
    """A fully explained bid recommendation for one campaign."""

    campaign_id: str
    bid_cpm: float
    max_cpc: float
    ecpm: float
    predicted_ctr: float
    predicted_cvr: float
    multiplier: float
    base_multiplier: float
    roas_multiplier: float
    confidence: float
    reasoning: str


def ecpm(predicted_ctr: float, predicted_cvr: float, target_cpa: float) -> float:
    """Expected revenue per thousand impressions.

    ``eCPM = pCTR * pCVR * targetCPA * 1000``
    """
    if min(predicted_ctr, predicted_cvr, target_cpa) < 0:
        msg = "ecpm inputs must be non-negative"
        raise ValueError(msg)
    return predicted_ctr * predicted_cvr * target_cpa * 1000.0


def max_cpc(predicted_cvr: float, target_cpa: float) -> float:
    """Highest cost-per-click that still meets the target CPA."""
    return predicted_cvr * target_cpa


def roas_multiplier(
    observed_roas: float, target_roas: float = DEFAULT_TARGET_ROAS
) -> tuple[float, str]:
    """Scale the bid up or down based on return against target."""
    if target_roas <= 0:
        return 1.0, "Target ROAS is not set, holding the bid flat"
    if observed_roas <= 0:
        return 0.6, "No observed revenue, cutting the bid to protect spend"

    ratio = observed_roas / target_roas
    if ratio >= 2.0:
        return 1.3, "ROAS is at least 2x target, raising the bid to win more volume"
    if ratio >= 1.2:
        return 1.1, "ROAS is above target, nudging the bid up"
    if ratio >= 0.8:
        return 1.0, "ROAS is on target, holding the bid"
    if ratio >= 0.5:
        return 0.8, "ROAS is below target, reducing the bid"
    return 0.6, "ROAS is far below target, cutting the bid hard"


def volume_multiplier(impressions: int, target_impressions: int = 10_000) -> float:
    """Ease bids for campaigns that have not yet exited the learning phase."""
    if target_impressions <= 0 or impressions >= target_impressions:
        return 1.0
    progress = impressions / target_impressions
    return clamp(0.85 + 0.15 * progress, 0.85, 1.0)


def recommend_bid(
    *,
    campaign_id: str,
    predicted_ctr: float,
    predicted_cvr: float,
    observed_roas: float,
    impressions: int,
    target_cpa: float = DEFAULT_TARGET_CPA,
    target_roas: float = DEFAULT_TARGET_ROAS,
    bid_cap_ratio: float = DEFAULT_BID_CAP_RATIO,
    learning_phase_impressions: int = 10_000,
) -> BidRecommendation:
    """Produce a capped, fully explained bid recommendation.

    The cap keeps a single campaign from bidding more than ``bid_cap_ratio`` of
    the target CPA per click, which bounds worst-case acquisition cost even if
    the predictions are optimistic.
    """
    effective_target_cpa = target_cpa if target_cpa > 0 else DEFAULT_TARGET_CPA
    expected_ecpm = ecpm(predicted_ctr, predicted_cvr, effective_target_cpa)

    base = roas_multiplier(observed_roas, target_roas)
    learning = volume_multiplier(impressions, learning_phase_impressions)
    multiplier = round(base[0] * learning, 4)

    # Per-impression value, then the strategy adjustment applied exactly once.
    raw_bid_cpm = expected_ecpm * multiplier
    cap_cpm = max_cpc(predicted_cvr, effective_target_cpa) * bid_cap_ratio * predicted_ctr * 1000.0

    bid_cpm = round(clamp(raw_bid_cpm, MIN_BID, max(cap_cpm, MIN_BID)), 4)
    capped = raw_bid_cpm > cap_cpm and cap_cpm >= MIN_BID

    reasoning = base[1]
    if learning < 1.0:
        reasoning += "; still in the learning phase so the bid is eased"
    if capped:
        reasoning += "; bid capped at " + str(int(bid_cap_ratio * 100)) + "% of target CPA value"

    confidence = round(
        clamp(
            0.35 + 0.45 * min(impressions / 50_000.0, 1.0) + (0.2 if observed_roas > 0 else 0.0),
            0.0,
            0.98,
        ),
        3,
    )

    return BidRecommendation(
        campaign_id=campaign_id,
        bid_cpm=bid_cpm,
        max_cpc=round(
            min(
                max_cpc(predicted_cvr, effective_target_cpa) * multiplier,
                effective_target_cpa * bid_cap_ratio,
            ),
            4,
        ),
        ecpm=round(expected_ecpm, 4),
        predicted_ctr=round(predicted_ctr, 6),
        predicted_cvr=round(predicted_cvr, 6),
        multiplier=multiplier,
        base_multiplier=base[0],
        roas_multiplier=base[0],
        confidence=confidence,
        reasoning=reasoning,
    )
