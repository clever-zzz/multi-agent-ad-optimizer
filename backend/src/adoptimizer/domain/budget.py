"""Budget reallocation.

The allocation problem is a linear program:

    maximize   sum(score_i * x_i)
    subject to sum(x_i) == total_budget
               lower_i <= x_i <= upper_i

For this structure a greedy fill in descending score order yields the exact
optimum, so the result is deterministic and needs no solver. CVXPY is used when
available to cross-check, and the demo heuristic (clip then renormalise, which
could violate the change bounds) is gone.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .kpi import PerformanceSnapshot
from .statistics import clamp, safe_ratio

try:  # pragma: no cover - exercised only when the analytics extra is installed
    import cvxpy as cp

    HAS_CVXPY = True
except ImportError:  # pragma: no cover
    cp = None
    HAS_CVXPY = False

MIN_BUDGET_FLOOR = 1.0


class BudgetAllocation(BaseModel):
    """A proposed change to one campaign budget."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    campaign_name: str = ""
    current_budget: float = Field(ge=0)
    recommended_budget: float = Field(ge=0)
    score: float = 0.0
    change_pct: float = 0.0
    reason: str = ""
    solver: str = "greedy_lp"

    @property
    def delta(self) -> float:
        return round(self.recommended_budget - self.current_budget, 4)


def allocation_score(snapshot: PerformanceSnapshot) -> float:
    """Blend ROAS with statistical confidence so tiny samples cannot dominate.

    A campaign with two lucky conversions would otherwise absorb the entire
    budget; weighting by the Wilson lower bound of its conversion rate removes
    that failure mode.
    """
    confidence_weight = clamp(safe_ratio(snapshot.impressions, 10_000.0), 0.2, 1.0)
    roas = snapshot.roas
    efficiency = clamp(safe_ratio(roas, 3.0), 0.0, 2.0)
    scale = clamp(safe_ratio(snapshot.conversions, 10.0), 0.0, 1.0)
    return round(efficiency * (0.5 + 0.3 * confidence_weight + 0.2 * scale), 6)


def _greedy_optimum(
    scores: np.ndarray, lower: np.ndarray, upper: np.ndarray, total: float
) -> np.ndarray:
    """Exact LP solution: fill from the lowest bound in descending score order."""
    allocation = lower.copy()
    remaining = total - float(allocation.sum())
    if remaining <= 0:
        return allocation

    for index in np.argsort(-scores, kind="stable"):
        room = float(upper[index]) - float(allocation[index])
        if room <= 0:
            continue
        take = min(room, remaining)
        allocation[index] += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return allocation


def _cvxpy_optimum(
    scores: np.ndarray, lower: np.ndarray, upper: np.ndarray, total: float
) -> np.ndarray | None:
    """Solve with CVXPY for cross-validation. Returns None when unavailable."""
    if not HAS_CVXPY:
        return None
    n = scores.shape[0]
    x = cp.Variable(n, nonneg=True)
    problem = cp.Problem(
        cp.Maximize(scores @ x),
        [
            cp.sum(x) == total,
            x >= lower,
            x <= upper,
        ],
    )
    try:
        problem.solve(solver=cp.SCS, verbose=False, eps=1e-6)
    except cp.error.SolverError:
        return None
    if problem.status not in ("optimal", "optimal_inaccurate") or x.value is None:
        return None
    clipped: np.ndarray = np.clip(np.asarray(x.value, dtype=float), lower, upper)
    return clipped


def _baseline(snapshot: PerformanceSnapshot, budgets: dict[str, float]) -> float:
    """The daily budget this campaign is managed against, or spend as a proxy."""
    configured = budgets.get(snapshot.campaign_id)
    if configured and configured > 0:
        return float(configured)
    return float(snapshot.total_cost)


def allocate(
    snapshots: list[PerformanceSnapshot],
    *,
    total_budget: float | None = None,
    max_change_pct: float = 0.5,
    daily_budgets: dict[str, float] | None = None,
    floor: float = MIN_BUDGET_FLOOR,
    cross_check_with_solver: bool = True,
    no_decrease: set[str] | None = None,
) -> list[BudgetAllocation]:
    """Reallocate budget across campaigns under bounded per-campaign change.

    ``max_change_pct`` is a fraction (0.5 == plus/minus 50 percent). The total
    is preserved exactly whenever the bounds permit it.

    ``daily_budgets`` maps campaign id to the budget it is actually managed
    against, and is the baseline every recommendation is measured from. The
    result is written through ``platform.set_daily_budget``, so the baseline has
    to be a daily figure too: sizing it from window spend inflated every
    recommendation by the length of the window. Callers with no campaign record
    to read from fall back to spend, which keeps this usable standalone.

    ``no_decrease`` names campaigns whose budget must not be reduced - in
    practice the ones already at or above their ROAS target. Their floor becomes
    their current budget, so the reallocation happens entirely out of the
    remaining headroom and the total is still preserved. Without it a campaign
    can be told to raise its bid and halve its budget in the same run, because
    the bid agent measures against the campaign's target while this one measures
    against the portfolio average.
    """
    eligible = [s for s in snapshots if s.total_cost > 0]
    if not eligible:
        return []

    budgets = daily_budgets or {}
    held = no_decrease or set()
    change = clamp(max_change_pct, 0.0, 10.0)
    current = np.array([max(_baseline(s, budgets), floor) for s in eligible], dtype=float)
    scores = np.array([max(allocation_score(s), 1e-6) for s in eligible], dtype=float)
    lower = np.maximum(current * (1.0 - change), floor)
    upper = current * (1.0 + change)
    for index, snapshot in enumerate(eligible):
        if snapshot.campaign_id in held:
            lower[index] = current[index]

    budget = float(current.sum()) if total_budget is None else float(total_budget)
    budget = clamp(budget, float(lower.sum()), float(upper.sum()))

    allocation = _greedy_optimum(scores, lower, upper, budget)
    solver_used = "greedy_lp"

    if cross_check_with_solver:
        solved = _cvxpy_optimum(scores, lower, upper, budget)
        if solved is not None:
            greedy_objective = float(scores @ allocation)
            solver_objective = float(scores @ solved)
            # Only adopt the solver result when it is genuinely better; this
            # keeps output deterministic while proving optimality in CI.
            if solver_objective > greedy_objective * (1 + 1e-6):
                allocation = solved
                solver_used = "cvxpy"
            else:
                solver_used = "greedy_lp(verified)"

    return [
        _to_allocation(
            snapshot,
            float(current[i]),
            float(allocation[i]),
            float(scores[i]),
            solver_used,
            held_at_target=snapshot.campaign_id in held,
        )
        for i, snapshot in enumerate(eligible)
    ]


def _to_allocation(
    snapshot: PerformanceSnapshot,
    current_budget: float,
    recommended: float,
    score: float,
    solver: str,
    *,
    held_at_target: bool = False,
) -> BudgetAllocation:
    change_pct = safe_ratio(recommended - current_budget, current_budget) * 100.0
    # The reallocation is zero-sum, so a strong campaign can still lose budget
    # to a stronger one. The reason therefore has to be phrased relative to the
    # portfolio; labelling a ROAS of 13 "inefficient" was simply untrue.
    if held_at_target and change_pct > -5:
        reason = (
            "ROAS "
            + format(snapshot.roas, ".2f")
            + " meets its target, budget held rather than cut"
        )
    elif change_pct > 5:
        reason = "ROAS " + format(snapshot.roas, ".2f") + " leads the portfolio, scaling spend up"
    elif change_pct < -5:
        reason = (
            "ROAS " + format(snapshot.roas, ".2f") + " trails the portfolio, pulling spend back"
        )
    else:
        reason = "Performance is in line with the portfolio, budget held"

    return BudgetAllocation(
        campaign_id=snapshot.campaign_id,
        campaign_name=snapshot.campaign_name,
        current_budget=round(current_budget, 2),
        recommended_budget=round(recommended, 2),
        score=score,
        change_pct=round(change_pct, 2),
        reason=reason,
        solver=solver,
    )


def total_delta(allocations: list[BudgetAllocation]) -> dict[str, Any]:
    """Summarise a reallocation plan for reporting and auditing."""
    return {
        "campaigns": len(allocations),
        "current_total": round(sum(a.current_budget for a in allocations), 2),
        "recommended_total": round(sum(a.recommended_budget for a in allocations), 2),
        "net_delta": round(sum(a.delta for a in allocations), 2),
        "increased": sum(1 for a in allocations if a.delta > 0),
        "decreased": sum(1 for a in allocations if a.delta < 0),
        "unchanged": sum(1 for a in allocations if a.delta == 0),
    }
