"""Key performance indicators for advertising campaigns.

Derived values are declared as pydantic computed fields so they survive
serialization. The demo version used plain properties, which meant every
API response and every LangGraph checkpoint silently dropped ctr/cvr/cpa/roas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .statistics import clamp, safe_ratio, wilson_interval

# Priors used to shrink sparse campaign data toward portfolio averages.
CTR_PRIOR_MEAN = 0.025
CTR_PRIOR_STRENGTH = 500.0
CVR_PRIOR_MEAN = 0.05
CVR_PRIOR_STRENGTH = 100.0

# Health score reference points (industry-typical "good" values).
HEALTH_CTR_REFERENCE = 0.05
HEALTH_CVR_REFERENCE = 0.10
HEALTH_CPA_REFERENCE = 200.0
HEALTH_ROAS_REFERENCE = 3.0

# The columns a daily aggregate can assert. Ingestion validation, the metric
# writer and the synthetic feed all read this one tuple, so measuring a new
# column is a change in one place instead of three that can drift apart.
DAILY_MEASUREMENTS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "conversions",
    "cost",
    "revenue",
    "unique_reach",
)


class PerformanceSnapshot(BaseModel):
    """Aggregated delivery performance for one campaign over one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: str
    campaign_name: str = ""
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)
    total_cost: float = Field(default=0.0, ge=0)
    total_revenue: float = Field(default=0.0, ge=0)
    window_start: datetime | None = None
    window_end: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _ignore_derived_values(cls, data: Any) -> Any:
        """Tolerate the computed fields that ``model_dump()`` writes out.

        Computed fields are serialised but are not constructor inputs, so with
        ``extra="forbid"`` a dump → validate round-trip is rejected outright. The
        agents do exactly that: metrics travel through the graph state and the
        LangGraph checkpointer as plain dicts and are rebuilt on every hop. The
        names are read off the model so this cannot drift when a derived metric is
        added, and genuine typos in upstream data are still refused.
        """
        if not isinstance(data, dict):
            return data
        derived = cls.model_computed_fields
        if not any(key in derived for key in data):
            return data
        return {key: value for key, value in data.items() if key not in derived}

    @model_validator(mode="after")
    def _check_funnel_consistency(self) -> Self:
        """Reject physically impossible funnels instead of trusting upstream."""
        if self.clicks > self.impressions:
            msg = "clicks cannot exceed impressions for campaign " + self.campaign_id
            raise ValueError(msg)
        if self.conversions > self.clicks:
            msg = "conversions cannot exceed clicks for campaign " + self.campaign_id
            raise ValueError(msg)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ctr(self) -> float:
        """Observed click-through rate."""
        return safe_ratio(self.clicks, self.impressions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cvr(self) -> float:
        """Observed click-to-conversion rate."""
        return safe_ratio(self.conversions, self.clicks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpa(self) -> float | None:
        """Cost per acquisition, or None when there were no conversions.

        Returning infinity here (as the demo did) produced invalid JSON because
        Infinity is not representable in strict JSON.
        """
        if self.conversions == 0:
            return None
        return round(self.total_cost / self.conversions, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def roas(self) -> float:
        """Return on ad spend."""
        return round(safe_ratio(self.total_revenue, self.total_cost), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpc(self) -> float:
        """Average cost per click."""
        return round(safe_ratio(self.total_cost, self.clicks), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpm(self) -> float:
        """Average cost per mille impressions."""
        return round(safe_ratio(self.total_cost, self.impressions) * 1000.0, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def predicted_ctr(self) -> float:
        """Shrunk CTR estimate suitable for bidding on sparse data."""
        return estimate_rate_for(self.clicks, self.impressions, CTR_PRIOR_MEAN, CTR_PRIOR_STRENGTH)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def predicted_cvr(self) -> float:
        """Shrunk CVR estimate suitable for bidding on sparse data."""
        return estimate_rate_for(self.conversions, self.clicks, CVR_PRIOR_MEAN, CVR_PRIOR_STRENGTH)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ctr_lower_bound(self) -> float:
        """Conservative CTR (Wilson lower bound) for ranking decisions."""
        return wilson_interval(self.clicks, self.impressions).lower

    def has_volume(self, min_impressions: int) -> bool:
        """Whether the snapshot carries enough data to act on."""
        return self.impressions >= min_impressions


def estimate_rate_for(
    successes: float, trials: float, prior_mean: float, prior_strength: float
) -> float:
    """Shrunk rate rounded to a stable precision."""
    from .statistics import estimate_rate

    return round(
        estimate_rate(successes, trials, prior_mean=prior_mean, prior_strength=prior_strength),
        6,
    )


class PortfolioSummary(BaseModel):
    """Aggregate view across every campaign in a run."""

    model_config = ConfigDict(frozen=True)

    campaigns: int = 0
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    total_cost: float = 0.0
    total_revenue: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ctr(self) -> float:
        return round(safe_ratio(self.clicks, self.impressions), 6)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cvr(self) -> float:
        return round(safe_ratio(self.conversions, self.clicks), 6)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cpa(self) -> float | None:
        if self.conversions == 0:
            return None
        return round(self.total_cost / self.conversions, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def roas(self) -> float:
        return round(safe_ratio(self.total_revenue, self.total_cost), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def health_score(self) -> float:
        return round(health_score(self), 2)


def summarize(snapshots: list[PerformanceSnapshot]) -> PortfolioSummary:
    """Fold campaign snapshots into a single portfolio summary."""
    return PortfolioSummary(
        campaigns=len(snapshots),
        impressions=sum(s.impressions for s in snapshots),
        clicks=sum(s.clicks for s in snapshots),
        conversions=sum(s.conversions for s in snapshots),
        total_cost=round(sum(s.total_cost for s in snapshots), 4),
        total_revenue=round(sum(s.total_revenue for s in snapshots), 4),
    )


def health_score(summary: PortfolioSummary) -> float:
    """Score delivery health from 0 to 100 across four equally weighted KPIs."""
    cpa_value = summary.cpa
    cpa_component = (
        1.0 if cpa_value is None else clamp(1.0 - cpa_value / HEALTH_CPA_REFERENCE, 0.0, 1.0)
    )

    score = 0.0
    score += clamp(summary.ctr / HEALTH_CTR_REFERENCE, 0.0, 1.0) * 25.0
    score += clamp(summary.cvr / HEALTH_CVR_REFERENCE, 0.0, 1.0) * 25.0
    score += cpa_component * 25.0
    score += clamp(summary.roas / HEALTH_ROAS_REFERENCE, 0.0, 1.0) * 25.0
    return score


def health_status(score: float) -> str:
    """Map a numeric health score onto a coarse status label."""
    if score >= 70:
        return "healthy"
    if score >= 45:
        return "warning"
    return "critical"


def snapshot_from_row(row: dict[str, Any]) -> PerformanceSnapshot:
    """Build a snapshot from a warehouse or repository row."""
    return PerformanceSnapshot(
        campaign_id=str(row["campaign_id"]),
        campaign_name=str(row.get("campaign_name", "")),
        impressions=int(row.get("impressions", 0) or 0),
        clicks=int(row.get("clicks", 0) or 0),
        conversions=int(row.get("conversions", 0) or 0),
        total_cost=float(row.get("total_cost", 0.0) or 0.0),
        total_revenue=float(row.get("total_revenue", 0.0) or 0.0),
        window_start=_parse_ts(row.get("window_start")),
        window_end=_parse_ts(row.get("window_end")),
    )


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None
