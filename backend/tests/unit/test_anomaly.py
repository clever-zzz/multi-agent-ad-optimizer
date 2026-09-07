"""Unit tests for rule based anomaly detection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adoptimizer.domain.anomaly import Alert, AlertThresholds, deduplicate, detect
from adoptimizer.domain.enums import AlertRule, AlertSeverity
from adoptimizer.domain.kpi import PerformanceSnapshot

# One snapshot that trips every rule at once.
BROKEN = PerformanceSnapshot(
    campaign_id="broken",
    impressions=10_000,
    clicks=20,
    conversions=1,
    total_cost=1_000.0,
    total_revenue=100.0,
)

HEALTHY = PerformanceSnapshot(
    campaign_id="healthy",
    impressions=10_000,
    clicks=500,
    conversions=50,
    total_cost=1_000.0,
    total_revenue=5_000.0,
)


def rules(alerts: list[Alert]) -> set[AlertRule]:
    return {alert.rule for alert in alerts}


class TestDetection:
    def test_healthy_campaign_raises_nothing(self) -> None:
        assert detect([HEALTHY]) == []

    def test_every_rule_fires_on_a_broken_campaign(self) -> None:
        alerts = detect(
            [BROKEN],
            daily_budgets={"broken": 100.0},
            reach_by_campaign={"broken": 100},
        )
        assert rules(alerts) == {
            AlertRule.LOW_CTR,
            AlertRule.HIGH_CPA,
            AlertRule.LOW_ROAS,
            AlertRule.BURN_RATE,
            AlertRule.FREQUENCY_FATIGUE,
        }

    def test_low_ctr(self) -> None:
        alerts = detect([BROKEN])
        alert = next(a for a in alerts if a.rule == AlertRule.LOW_CTR)
        assert alert.observed == pytest.approx(0.002)
        assert alert.threshold == pytest.approx(0.005)
        assert alert.severity == AlertSeverity.CRITICAL

    def test_high_cpa(self) -> None:
        alert = next(a for a in detect([BROKEN]) if a.rule == AlertRule.HIGH_CPA)
        assert alert.observed == pytest.approx(1_000.0)

    def test_low_roas(self) -> None:
        alert = next(a for a in detect([BROKEN]) if a.rule == AlertRule.LOW_ROAS)
        assert alert.observed == pytest.approx(0.1)

    def test_burn_rate_needs_a_budget(self) -> None:
        assert AlertRule.BURN_RATE not in rules(detect([BROKEN]))
        assert AlertRule.BURN_RATE in rules(detect([BROKEN], daily_budgets={"broken": 100.0}))

    def test_frequency_needs_reach(self) -> None:
        assert AlertRule.FREQUENCY_FATIGUE not in rules(detect([BROKEN]))
        assert AlertRule.FREQUENCY_FATIGUE in rules(
            detect([BROKEN], reach_by_campaign={"broken": 100})
        )

    def test_burn_rate_escalates_to_critical_when_far_over(self) -> None:
        warning = detect([BROKEN], daily_budgets={"broken": 700.0})
        critical = detect([BROKEN], daily_budgets={"broken": 100.0})
        burn_w = next(a for a in warning if a.rule == AlertRule.BURN_RATE)
        burn_c = next(a for a in critical if a.rule == AlertRule.BURN_RATE)
        assert burn_w.severity == AlertSeverity.WARNING
        assert burn_c.severity == AlertSeverity.CRITICAL


class TestEvidenceGates:
    def test_low_impressions_suppress_the_ctr_rule(self) -> None:
        quiet = PerformanceSnapshot(
            campaign_id="quiet", impressions=50, clicks=0, conversions=0, total_cost=0.0
        )
        assert AlertRule.LOW_CTR not in rules(detect([quiet]))

    def test_no_conversions_suppress_the_cpa_rule(self) -> None:
        no_conv = PerformanceSnapshot(
            campaign_id="nc", impressions=10_000, clicks=20, conversions=0, total_cost=1_000.0
        )
        assert AlertRule.HIGH_CPA not in rules(detect([no_conv]))

    def test_too_few_clicks_suppress_the_cpa_rule(self) -> None:
        few_clicks = PerformanceSnapshot(
            campaign_id="fc", impressions=10_000, clicks=5, conversions=1, total_cost=1_000.0
        )
        assert AlertRule.HIGH_CPA not in rules(detect([few_clicks]))

    def test_low_spend_suppress_the_roas_rule(self) -> None:
        cheap = PerformanceSnapshot(
            campaign_id="cheap", impressions=1_000, clicks=100, conversions=1, total_cost=50.0
        )
        assert AlertRule.LOW_ROAS not in rules(detect([cheap]))


class TestThresholds:
    def test_thresholds_are_tunable_without_code_changes(self) -> None:
        strict = AlertThresholds(ctr_floor=0.10)
        assert AlertRule.LOW_CTR in rules(detect([HEALTHY], strict))

    def test_relaxed_thresholds_silence_the_alert(self) -> None:
        relaxed = AlertThresholds(ctr_floor=0.001, cpa_ceiling=5_000.0, roas_floor=0.0)
        assert detect([BROKEN], relaxed) == []


class TestOrderingAndDedup:
    def test_results_are_sorted_by_severity_then_campaign(self) -> None:
        alerts = detect(
            [BROKEN, HEALTHY],
            daily_budgets={"broken": 100.0},
            reach_by_campaign={"broken": 100},
        )
        ranks = [
            {AlertSeverity.CRITICAL: 0, AlertSeverity.WARNING: 1, AlertSeverity.INFO: 2}[a.severity]
            for a in alerts
        ]
        assert ranks == sorted(ranks)

    def test_dedup_key_is_stable_across_runs(self) -> None:
        first = detect([BROKEN])
        second = detect([BROKEN])
        assert [a.dedup_key for a in first] == [a.dedup_key for a in second]

    def test_deduplicate_keeps_the_most_severe(self) -> None:
        base = {
            "campaign_id": "c1",
            "rule": AlertRule.LOW_CTR,
            "observed": 0.001,
            "threshold": 0.005,
            "message": "m",
            "dedup_key": "c1:low_ctr",
        }
        mild = Alert(severity=AlertSeverity.INFO, **base)
        severe = Alert(severity=AlertSeverity.CRITICAL, **base)
        kept = deduplicate([mild, severe, mild])
        assert len(kept) == 1
        assert kept[0].severity == AlertSeverity.CRITICAL

    def test_deduplicate_preserves_distinct_findings(self) -> None:
        alerts = detect([BROKEN])
        assert len(deduplicate(alerts)) == len(alerts)


class TestSerialization:
    def test_to_dict_is_json_shaped(self) -> None:
        payload = detect([BROKEN])[0].to_dict()
        assert set(payload) == {
            "campaign_id",
            "rule",
            "severity",
            "observed",
            "threshold",
            "message",
            "dedup_key",
            "context",
            "detected_at",
        }
        assert isinstance(payload["rule"], str)
        # Must round-trip through datetime.isoformat, not a raw datetime object.
        datetime.fromisoformat(payload["detected_at"])

    def test_detected_at_is_timezone_aware(self) -> None:
        assert detect([BROKEN])[0].detected_at.tzinfo is not None

    def test_fingerprint_matches_dedup_key(self) -> None:
        alert = detect([BROKEN])[0]
        assert alert.fingerprint == alert.dedup_key

    def test_context_carries_the_evidence(self) -> None:
        alert = next(a for a in detect([BROKEN]) if a.rule == AlertRule.LOW_CTR)
        assert alert.context["impressions"] == 10_000
        assert alert.context["clicks"] == 20


def test_alerts_default_to_utc_now() -> None:
    before = datetime.now(UTC)
    alert = detect([BROKEN])[0]
    assert alert.detected_at >= before
