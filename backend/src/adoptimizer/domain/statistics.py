"""Statistical estimators used by the agents.

Point estimates computed from small samples are extremely noisy, which is what
made the demo version unstable. Every rate here is shrunk toward a prior using
empirical Bayes, and comparisons use Wilson score intervals so a campaign with
three conversions is not treated as a proven winner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Two-sided 95% normal quantile.
Z_95 = 1.959963984540054


def estimate_rate(
    successes: float,
    trials: float,
    *,
    prior_mean: float,
    prior_strength: float,
) -> float:
    """Empirical-Bayes shrunk rate estimate.

    Returns ``(successes + prior_strength * prior_mean) / (trials + prior_strength)``.
    With no data the estimate equals the prior; with abundant data it converges
    to the raw observed rate.
    """
    if trials < 0 or prior_strength < 0:
        msg = "trials and prior_strength must be non-negative"
        raise ValueError(msg)
    denominator = trials + prior_strength
    if denominator <= 0:
        return prior_mean
    return (successes + prior_strength * prior_mean) / denominator


def safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Divide guarding against a zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    """Wilson score confidence interval for a binomial proportion."""

    lower: float
    upper: float
    point: float

    @property
    def width(self) -> float:
        return self.upper - self.lower


def wilson_interval(
    successes: int, trials: int, *, z: float = Z_95, prior: float = 0.0
) -> WilsonInterval:
    """Wilson score interval, more accurate than the normal approximation."""
    if trials <= 0:
        return WilsonInterval(lower=prior, upper=prior, point=prior)

    z2 = z * z
    centre = successes + z2 / 2.0
    denominator = trials + z2
    spread = z * math.sqrt(max(successes * (trials - successes) / trials + z2 / 4.0, 0.0))

    lower = max(0.0, (centre - spread) / denominator)
    upper = min(1.0, (centre + spread) / denominator)
    return WilsonInterval(lower=lower, upper=upper, point=successes / trials)


def is_significantly_better(
    winner_successes: int,
    winner_trials: int,
    loser_successes: int,
    loser_trials: int,
    *,
    z: float = Z_95,
) -> bool:
    """Two-proportion z-test: is the winner reliably better than the loser?"""
    if winner_trials <= 0 or loser_trials <= 0:
        return False

    p1 = winner_successes / winner_trials
    p2 = loser_successes / loser_trials
    pooled = (winner_successes + loser_successes) / (winner_trials + loser_trials)
    if pooled in (0.0, 1.0):
        return False

    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / winner_trials + 1.0 / loser_trials))
    if se == 0:
        return False
    return (p1 - p2) / se > z


def required_sample_size(
    baseline_rate: float, minimum_detectable_effect: float, *, z: float = Z_95
) -> int:
    """Per-arm sample size for a two-sided proportion test at 80% power."""
    if baseline_rate <= 0 or minimum_detectable_effect <= 0:
        return 0
    p1 = baseline_rate
    p2 = baseline_rate * (1.0 + minimum_detectable_effect)
    if p2 >= 1.0:
        return 0
    p_bar = (p1 + p2) / 2.0
    z_beta = 0.8416212335729143
    numerator = (
        z * math.sqrt(2.0 * p_bar * (1.0 - p_bar))
        + z_beta * math.sqrt(p1 * (1.0 - p1) + p2 * (1.0 - p2))
    ) ** 2
    return math.ceil(numerator / ((p2 - p1) ** 2))


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a value into an inclusive range."""
    if low > high:
        msg = "low must not exceed high"
        raise ValueError(msg)
    return max(low, min(high, value))
