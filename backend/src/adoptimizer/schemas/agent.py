"""Contracts produced by the agents and consumed by the API and UI."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.enums import ActionStatus, ActionType, AlertRule, AlertSeverity, AlertStatus


class AgentMessage(BaseModel):
    """A human-readable line in the run narrative."""

    model_config = ConfigDict(frozen=True)

    agent: str
    content: str
    iteration: int = 0
    created_at: datetime | None = None


class CreativeVariant(BaseModel):
    """One generated creative candidate.

    This is also the schema handed to the model gateway, so field constraints
    double as the output contract enforced on LLM responses.
    """

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(min_length=4, max_length=120)
    description: str = Field(default="", max_length=600)
    cta_text: str = Field(default="Learn More", max_length=40)
    target_emotion: Literal["urgency", "trust", "curiosity", "benefit", "social_proof"] = "benefit"
    ab_group: str = Field(default="variant", max_length=40)
    rationale: str = Field(default="", max_length=400)
    source: Literal["llm", "rule"] = "rule"

    @field_validator("headline", "description", "cta_text", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class BiddingDecisionOut(BaseModel):
    """A bid recommendation with the reasoning needed to justify it."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    bid_cpm: float
    max_cpc: float
    ecpm: float
    predicted_ctr: float
    predicted_cvr: float
    multiplier: float
    confidence: float
    reasoning: str = ""


class OptimizationActionOut(BaseModel):
    """A proposed change awaiting approval or execution."""

    id: str | None = None
    run_id: str | None = None
    campaign_id: str
    creative_id: str | None = None
    action_type: ActionType
    status: ActionStatus = ActionStatus.PROPOSED
    before_value: str = ""
    after_value: str = ""
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    proposed_by: str = "optimize"
    # Which way this proposal moves spend, and the reference frame it was
    # decided against. Both are what let the critic - and the approval screen -
    # tell an opposing pair (raise the bid, halve the budget) from a coherent
    # one, instead of having to parse the free-text reason.
    direction: str = ""
    basis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    approved_by: str | None = None
    executed_at: datetime | None = None
    external_reference: str | None = None
    error_message: str | None = None


class CriticFindingOut(BaseModel):
    """One reconciliation verdict, exposed so the override is actionable."""

    id: str | None = None
    run_id: str | None = None
    iteration: int = 0
    kind: str
    scope: str = "campaign"
    campaign_id: str = ""
    creative_id: str | None = None
    kept_action_id: str | None = None
    kept_action_type: str = ""
    kept_confidence: float = 0.0
    reason: str = ""
    suppressed_action_ids: list[str] = Field(default_factory=list)
    suppressed_actions: list[dict[str, Any]] = Field(default_factory=list)
    escalate: bool = False
    created_at: datetime | None = None


class AlertOut(BaseModel):
    """A monitoring alert as exposed by the API."""

    id: str | None = None
    campaign_id: str
    run_id: str | None = None
    rule: AlertRule
    severity: AlertSeverity
    status: AlertStatus = AlertStatus.OPEN
    observed: float | None = None
    threshold: float
    message: str
    dedup_key: str
    context: dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None


class AlertTrendPoint(BaseModel):
    """Alert volume for one rule and severity pair inside the summary window."""

    rule: str
    severity: str
    total: int


class AlertSummaryOut(BaseModel):
    """Counts backing the alerts console badges, KPI tiles and trend table."""

    # Serialised as "open": the field name avoids shadowing the builtin while the
    # wire contract keeps the short key the console already reads.
    open_alerts: int = Field(
        default=0,
        serialization_alias="open",
        description="Alerts that are not resolved yet (open plus acknowledged)",
    )
    acknowledged: int = 0
    resolved: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    history: list[AlertTrendPoint] = Field(default_factory=list)


class RunSummaryOut(BaseModel):
    """Outcome block persisted with a completed run."""

    status: str = "pending"
    iterations: int = 0
    campaigns: int = 0
    creatives_generated: int = 0
    bidding_decisions: int = 0
    budget_adjustments: int = 0
    actions: int = 0
    # `actions` is the post-review count. The pair below lets an operator see
    # how much the critic withheld, and why the queue is shorter than the raw
    # proposal count. Both default to 0 for runs persisted before the critic.
    actions_proposed: int = 0
    actions_suppressed: int = 0
    critic_findings: int = 0
    # Which rules did the withholding, so the summary says more than "some
    # proposals were dropped". The findings themselves live in critic_findings.
    critic_findings_by_kind: dict[str, int] = Field(default_factory=dict)
    action_counts: dict[str, int] = Field(default_factory=dict)
    alerts_raised: int = 0
    health: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    # Tool-layer counters for this run: how many calls were made, how many were
    # dry-run preflights, and whether agent writes were permitted at all.
    tools: dict[str, Any] = Field(default_factory=dict)


class BudgetAllocationOut(BaseModel):
    """A budget reallocation proposal."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    campaign_name: str = ""
    current_budget: float
    recommended_budget: float
    change_pct: float
    score: float = 0.0
    reason: str = ""
    solver: str = "greedy_lp"
