"""Rule-based anomaly detection.

Rules are declarative data rather than hard-coded branches so operators can
tune thresholds per account without a code change. Every alert carries a stable
deduplication key so repeated runs do not spam the same finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .enums import AlertRule, AlertSeverity
from .kpi import PerformanceSnapshot
from .statistics import safe_ratio


@dataclass(frozen=True, slots=True)
class AlertThresholds:
    """Tunable detection thresholds."""

    ctr_floor: float = 0.005
    cpa_ceiling: float = 200.0
    roas_floor: float = 1.0
    min_impressions: int = 100
    min_clicks: int = 20
    min_spend_for_roas: float = 100.0
    burn_rate_multiplier: float = 1.25
    # The burn rate at which the rule calls itself critical, named rather than
    # written as `burn_rate_multiplier * 1.5`. It is a business decision - how
    # far over budget is an emergency - and it used to be a magic factor nobody
    # could tune without editing the detector.
    burn_rate_critical_multiplier: float = 1.875
    # Once a campaign is critical it stays critical until its ratio falls this
    # far back below the critical line, so a ratio hovering at the boundary does
    # not flip classification every run.
    burn_rate_hysteresis: float = 0.05
    frequency_ceiling: float = 6.0


@dataclass(frozen=True, slots=True)
class Alert:
    """A single detected anomaly."""

    campaign_id: str
    rule: AlertRule
    severity: AlertSeverity
    observed: float | None
    threshold: float
    message: str
    dedup_key: str
    context: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # How far past its threshold the observation is, as a relative gap. Severity
    # collapses this into three levels, which is what makes 1.857 and 1.875 of a
    # daily budget look like different worlds. The continuous value is kept so
    # downstream reconciliation can weigh near-boundary cases proportionally.
    urgency: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise for persistence and API responses."""
        return {
            "campaign_id": self.campaign_id,
            "rule": self.rule.value,
            "severity": self.severity.value,
            "observed": self.observed,
            "threshold": self.threshold,
            "urgency": round(self.urgency, 4),
            "message": self.message,
            "dedup_key": self.dedup_key,
            "context": self.context,
            "detected_at": self.detected_at.isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        """Stable identity used to suppress duplicate alerts."""
        return self.dedup_key


CRITICAL_GAP = 0.5
WARNING_GAP = 0.2


def _gap(observed: float, threshold: float, *, higher_is_worse: bool) -> float:
    """Relative distance past the threshold; negative when it is not breached.

    This is the continuous quantity the severity bands are cut out of. Keeping
    it rather than discarding it is what lets a caller distinguish "just over"
    from "far over" without inventing a fourth severity level.
    """
    if threshold == 0:
        return 0.0
    return (
        (observed - threshold) / threshold
        if higher_is_worse
        else (threshold - observed) / threshold
    )


def _severity_from_gap(gap: float) -> AlertSeverity:
    """Escalate severity with the relative distance past the threshold."""
    if gap >= CRITICAL_GAP:
        return AlertSeverity.CRITICAL
    if gap >= WARNING_GAP:
        return AlertSeverity.WARNING
    return AlertSeverity.INFO


def detect(
    snapshots: list[PerformanceSnapshot],
    thresholds: AlertThresholds | None = None,
    *,
    daily_budgets: dict[str, float] | None = None,
    reach_by_campaign: dict[str, int] | None = None,
    window_days: int = 1,
    prior_severities: dict[str, str] | None = None,
) -> list[Alert]:
    """Run every rule over the supplied snapshots.

    ``window_days`` is the span the snapshots were aggregated over. Rules that
    weigh spend against a *daily* budget need it: a snapshot carries window
    totals only, so without the span a 21-day total reads as a single day of
    spend and every campaign looks like a runaway spender.

    ``prior_severities`` maps campaign id to the severity this rule reported last
    pass, and is used only to keep a burn-rate alert critical once it is critical
    rather than letting it flip at the boundary.
    """
    cfg = thresholds or AlertThresholds()
    budgets = daily_budgets or {}
    reach = reach_by_campaign or {}
    prior = prior_severities or {}
    days = max(1, int(window_days))
    alerts: list[Alert] = []

    for snapshot in snapshots:
        alerts.extend(_check_ctr(snapshot, cfg))
        alerts.extend(_check_cpa(snapshot, cfg))
        alerts.extend(_check_roas(snapshot, cfg))
        alerts.extend(
            _check_burn_rate(snapshot, cfg, budgets, days, prior.get(snapshot.campaign_id))
        )
        alerts.extend(_check_frequency(snapshot, cfg, reach))

    return sorted(alerts, key=lambda a: (_severity_rank(a.severity), a.campaign_id, a.rule.value))


def _severity_rank(severity: AlertSeverity) -> int:
    return {AlertSeverity.CRITICAL: 0, AlertSeverity.WARNING: 1, AlertSeverity.INFO: 2}[severity]


def _check_ctr(snapshot: PerformanceSnapshot, cfg: AlertThresholds) -> list[Alert]:
    if snapshot.impressions < cfg.min_impressions:
        return []
    if snapshot.ctr >= cfg.ctr_floor:
        return []
    gap = _gap(snapshot.ctr, cfg.ctr_floor, higher_is_worse=False)
    return [
        Alert(
            campaign_id=snapshot.campaign_id,
            rule=AlertRule.LOW_CTR,
            severity=_severity_from_gap(gap),
            urgency=gap,
            observed=round(snapshot.ctr, 6),
            threshold=cfg.ctr_floor,
            message=(
                "Campaign "
                + snapshot.campaign_id
                + " CTR "
                + format(snapshot.ctr, ".3%")
                + " is below the "
                + format(cfg.ctr_floor, ".3%")
                + " floor"
            ),
            dedup_key=snapshot.campaign_id + ":" + AlertRule.LOW_CTR.value,
            context={"impressions": snapshot.impressions, "clicks": snapshot.clicks},
        )
    ]


def _check_cpa(snapshot: PerformanceSnapshot, cfg: AlertThresholds) -> list[Alert]:
    if snapshot.conversions <= 0 or snapshot.clicks < cfg.min_clicks:
        return []
    cpa = snapshot.total_cost / snapshot.conversions
    if cpa <= cfg.cpa_ceiling:
        return []
    gap = _gap(cpa, cfg.cpa_ceiling, higher_is_worse=True)
    return [
        Alert(
            campaign_id=snapshot.campaign_id,
            rule=AlertRule.HIGH_CPA,
            severity=_severity_from_gap(gap),
            urgency=gap,
            observed=round(cpa, 2),
            threshold=cfg.cpa_ceiling,
            message=(
                "Campaign "
                + snapshot.campaign_id
                + " CPA "
                + format(cpa, ".2f")
                + " exceeds the "
                + format(cfg.cpa_ceiling, ".2f")
                + " ceiling"
            ),
            dedup_key=snapshot.campaign_id + ":" + AlertRule.HIGH_CPA.value,
            context={"conversions": snapshot.conversions, "total_cost": snapshot.total_cost},
        )
    ]


def _check_roas(snapshot: PerformanceSnapshot, cfg: AlertThresholds) -> list[Alert]:
    if snapshot.total_cost < cfg.min_spend_for_roas:
        return []
    if snapshot.roas >= cfg.roas_floor:
        return []
    gap = _gap(snapshot.roas, cfg.roas_floor, higher_is_worse=False)
    return [
        Alert(
            campaign_id=snapshot.campaign_id,
            rule=AlertRule.LOW_ROAS,
            severity=_severity_from_gap(gap),
            urgency=gap,
            observed=round(snapshot.roas, 4),
            threshold=cfg.roas_floor,
            message=(
                "Campaign "
                + snapshot.campaign_id
                + " ROAS "
                + format(snapshot.roas, ".2f")
                + " is below the "
                + format(cfg.roas_floor, ".2f")
                + " target"
            ),
            dedup_key=snapshot.campaign_id + ":" + AlertRule.LOW_ROAS.value,
            context={"total_cost": snapshot.total_cost, "total_revenue": snapshot.total_revenue},
        )
    ]


def _check_burn_rate(
    snapshot: PerformanceSnapshot,
    cfg: AlertThresholds,
    budgets: dict[str, float],
    window_days: int = 1,
    prior_severity: str | None = None,
) -> list[Alert]:
    """Compare average daily spend against the campaign's daily budget.

    ``total_cost`` covers the whole window, so it has to be normalised before
    the comparison. Dividing a multi-day total by a one-day budget is what made
    every campaign in a long run trip this rule at once.

    The critical line is ``burn_rate_critical_multiplier``, and a campaign that
    was critical last pass stays critical until it falls a hysteresis band back
    below it. Without that, a ratio sitting on the line reclassifies every run,
    and with it whether the reconciliation step is allowed to overrule the
    anomaly at all.
    """
    daily_budget = budgets.get(snapshot.campaign_id)
    if not daily_budget or daily_budget <= 0:
        return []
    days = max(1, window_days)
    daily_spend = snapshot.total_cost / days
    ratio = safe_ratio(daily_spend, daily_budget)
    if ratio <= cfg.burn_rate_multiplier:
        return []
    critical_at = cfg.burn_rate_critical_multiplier
    held_critical = prior_severity == AlertSeverity.CRITICAL.value and ratio >= critical_at * (
        1.0 - cfg.burn_rate_hysteresis
    )
    severity = (
        AlertSeverity.CRITICAL if held_critical or ratio >= critical_at else AlertSeverity.WARNING
    )
    gap = _gap(ratio, cfg.burn_rate_multiplier, higher_is_worse=True)
    return [
        Alert(
            campaign_id=snapshot.campaign_id,
            rule=AlertRule.BURN_RATE,
            severity=severity,
            urgency=gap,
            observed=round(ratio, 3),
            threshold=cfg.burn_rate_multiplier,
            message=(
                "Campaign "
                + snapshot.campaign_id
                + " is averaging "
                + format(ratio, ".0%")
                + " of its daily budget over "
                + str(days)
                + " days"
            ),
            dedup_key=snapshot.campaign_id + ":" + AlertRule.BURN_RATE.value,
            context={
                "daily_budget": daily_budget,
                "total_cost": snapshot.total_cost,
                "daily_spend": round(daily_spend, 2),
                "window_days": days,
                "critical_multiplier": critical_at,
            },
        )
    ]


def _check_frequency(
    snapshot: PerformanceSnapshot, cfg: AlertThresholds, reach: dict[str, int]
) -> list[Alert]:
    unique_reach = reach.get(snapshot.campaign_id)
    if not unique_reach or unique_reach <= 0:
        return []
    frequency = snapshot.impressions / unique_reach
    if frequency <= cfg.frequency_ceiling:
        return []
    gap = _gap(frequency, cfg.frequency_ceiling, higher_is_worse=True)
    return [
        Alert(
            campaign_id=snapshot.campaign_id,
            rule=AlertRule.FREQUENCY_FATIGUE,
            severity=_severity_from_gap(gap),
            urgency=gap,
            observed=round(frequency, 3),
            threshold=cfg.frequency_ceiling,
            message=(
                "Campaign "
                + snapshot.campaign_id
                + " frequency "
                + format(frequency, ".2f")
                + " suggests creative fatigue"
            ),
            dedup_key=snapshot.campaign_id + ":" + AlertRule.FREQUENCY_FATIGUE.value,
            context={"impressions": snapshot.impressions, "unique_reach": unique_reach},
        )
    ]


def deduplicate(alerts: list[Alert]) -> list[Alert]:
    """Collapse repeated fingerprints, keeping the most severe occurrence."""
    best: dict[str, Alert] = {}
    for alert in alerts:
        current = best.get(alert.fingerprint)
        if current is None or _severity_rank(alert.severity) < _severity_rank(current.severity):
            best[alert.fingerprint] = alert
    return list(best.values())
