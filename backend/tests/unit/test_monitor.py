"""Unit tests for the monitor agent's burn-rate hysteresis plumbing.

The domain rule is tested in ``test_anomaly``; what matters here is that the
monitor actually feeds it last pass's verdicts. Without that wiring the
hysteresis band is dead code and a ratio sitting on the critical line flips
classification every run, which in turn flips whether reconciliation is allowed
to overrule the anomaly.
"""

from __future__ import annotations

from typing import Any

from adoptimizer.agents.monitor import MonitorAgent
from adoptimizer.orchestrator.state import initial_state


def state_with_alerts(alerts: list[Any]) -> Any:
    state = initial_state(run_id="run_monitor")
    state["alerts"] = alerts
    return state


class TestPriorBurnRate:
    def test_collects_only_the_burn_rate_verdicts(self) -> None:
        state = state_with_alerts(
            [
                {"rule": "burn_rate", "campaign_id": "camp_a", "severity": "critical"},
                {"rule": "low_ctr", "campaign_id": "camp_b", "severity": "critical"},
                {"rule": "burn_rate", "campaign_id": "camp_c", "severity": "warning"},
            ]
        )

        assert MonitorAgent._prior_burn_rate(state) == {
            "camp_a": "critical",
            "camp_c": "warning",
        }

    def test_the_first_iteration_has_no_prior(self) -> None:
        """The channel is empty on the first pass, which is the common case."""
        assert MonitorAgent._prior_burn_rate(initial_state()) == {}

    def test_a_replayed_channel_cannot_break_the_reader(self) -> None:
        """A checkpointed run restores this channel from JSON, shape unchecked."""
        state = state_with_alerts(
            ["junk", {"rule": "burn_rate", "campaign_id": "camp_a"}, {"severity": "critical"}]
        )

        assert MonitorAgent._prior_burn_rate(state) == {"camp_a": ""}
