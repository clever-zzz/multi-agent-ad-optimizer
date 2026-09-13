"""HTTP request and response contracts.

These are deliberately separate from the ORM models: the API surface is a
versioned contract, and leaking ORM objects into responses couples the wire
format to the database schema.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from ..domain.enums import (
    ABTestStatus,
    CampaignStatus,
    CreativeStatus,
    Platform,
    Role,
    RunStatus,
)
from .agent import (
    BudgetAllocationOut,
    CriticFindingOut,
    OptimizationActionOut,
    RunSummaryOut,
)


class LoginRequest(BaseModel):
    """Credential submission."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class RefreshRequest(BaseModel):
    """Refresh token rotation."""

    refresh_token: str = Field(min_length=20)


class TokenResponse(BaseModel):
    """Issued tokens plus the authenticated profile."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  # OAuth 2.0 field name
    expires_in: int
    user: UserOut


class UserOut(BaseModel):
    """Public representation of an account. Never carries the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str = ""
    role: Role
    is_active: bool = True
    must_change_password: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class CreateUserRequest(BaseModel):
    """Administrator account creation."""

    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    role: Role = Role.VIEWER
    full_name: str = Field(default="", max_length=200)


class UpdateUserRoleRequest(BaseModel):
    role: Role


class AdminUserUpdateRequest(BaseModel):
    """Administrator changes to an existing account.

    At least one field must be supplied; an empty patch is rejected rather than
    silently succeeding.
    """

    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> AdminUserUpdateRequest:
        if self.role is None and self.is_active is None:
            msg = "Provide at least one of role or is_active"
            raise ValueError(msg)
        return self


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class CampaignCreate(BaseModel):
    """Payload for creating a campaign."""

    name: str = Field(min_length=2, max_length=300)
    platform: Platform
    daily_budget: float = Field(gt=0, le=10_000_000)
    total_budget: float = Field(default=0.0, ge=0, le=100_000_000)
    target_cpa: float = Field(default=100.0, gt=0, le=1_000_000)
    target_roas: float = Field(default=2.0, gt=0, le=100)
    start_date: date | None = None
    end_date: date | None = None
    objective: str = Field(default="conversions", max_length=60)
    target_audience: str = Field(default="", max_length=2000)
    external_id: str | None = Field(default=None, max_length=120)

    @field_validator("end_date")
    @classmethod
    def _validate_window(cls, value: date | None, info: Any) -> date | None:
        start = info.data.get("start_date")
        if value and start and value < start:
            msg = "end_date cannot precede start_date"
            raise ValueError(msg)
        return value


class CampaignUpdate(BaseModel):
    """Partial campaign update."""

    name: str | None = Field(default=None, min_length=2, max_length=300)
    status: CampaignStatus | None = None
    daily_budget: float | None = Field(default=None, gt=0, le=10_000_000)
    total_budget: float | None = Field(default=None, ge=0, le=100_000_000)
    target_cpa: float | None = Field(default=None, gt=0, le=1_000_000)
    target_roas: float | None = Field(default=None, gt=0, le=100)
    end_date: date | None = None
    objective: str | None = Field(default=None, max_length=60)
    target_audience: str | None = Field(default=None, max_length=2000)
    external_id: str | None = Field(default=None, max_length=120)


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    platform: Platform
    status: CampaignStatus
    external_id: str | None = None
    daily_budget: float
    total_budget: float
    target_cpa: float
    target_roas: float
    current_bid_cpm: float = 0.0
    start_date: date
    end_date: date | None = None
    objective: str = ""
    target_audience: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreativeCreate(BaseModel):
    campaign_id: str | None = None
    headline: str = Field(min_length=4, max_length=300)
    description: str = Field(default="", max_length=2000)
    cta_text: str = Field(default="Learn More", max_length=60)
    creative_type: Literal["text", "image", "video"] = "text"
    target_emotion: str = Field(default="", max_length=40)
    ab_group: str = Field(default="control", max_length=40)
    origin: Literal["human", "llm", "rule"] = "human"
    status: CreativeStatus = CreativeStatus.DRAFT


class CreativeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    campaign_id: str
    headline: str
    description: str = ""
    cta_text: str = ""
    creative_type: str = "text"
    target_emotion: str = ""
    status: CreativeStatus
    ab_group: str = "control"
    origin: str = "human"
    score: float | None = None
    generated_by_run_id: str | None = None
    created_at: datetime | None = None


class CreativeStatusUpdate(BaseModel):
    status: CreativeStatus


class RunStartRequest(BaseModel):
    """Trigger an optimization run."""

    campaign_ids: list[str] | None = Field(default=None, max_length=500)
    max_iterations: int | None = Field(default=None, ge=1, le=10)
    window_days: int = Field(default=7, ge=1, le=90)
    background: bool = Field(
        default=True, description="Return immediately and stream progress, or wait for completion"
    )


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: RunStatus
    trigger_type: str
    requested_by: str | None = None
    campaign_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    iteration: int = 0
    max_iterations: int = 3
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    summary: RunSummaryOut | dict[str, Any] = Field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_cost_usd: float = 0.0
    created_at: datetime | None = None


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    seq: int
    agent: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RunDetailOut(BaseModel):
    """Full run view: metadata, timeline, actions, budget plan and verdicts."""

    run: RunOut
    events: list[RunEventOut] = Field(default_factory=list)
    actions: list[OptimizationActionOut] = Field(default_factory=list)
    allocations: list[BudgetAllocationOut] = Field(default_factory=list)
    findings: list[CriticFindingOut] = Field(default_factory=list)


class ActionDecisionRequest(BaseModel):
    """Approve or reject with an optional operator note."""

    reason: str = Field(default="", max_length=500)


class BulkActionRequest(BaseModel):
    action_ids: list[str] = Field(min_length=1, max_length=200)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    execute: bool = False


class BulkSkippedEntry(BaseModel):
    """One batch member left untouched, with the machine-readable reason.

    Reasons: ``not_found``, ``status_<current>``, ``low_confidence``.
    """

    id: str
    reason: str


class BulkFailedEntry(BaseModel):
    """One batch member that raised while executing, so the batch could continue."""

    id: str
    error: str


class BulkActionResult(BaseModel):
    approved: list[str] = Field(default_factory=list)
    executed: list[str] = Field(default_factory=list)
    skipped: list[BulkSkippedEntry] = Field(default_factory=list)
    failed: list[BulkFailedEntry] = Field(default_factory=list)


class AlertAcknowledgeRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ABTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    campaign_id: str
    name: str
    hypothesis: str = ""
    status: ABTestStatus
    control_creative_id: str | None = None
    variant_creative_id: str | None = None
    metric: str = "ctr"
    minimum_detectable_effect: float = 0.1
    required_sample_size: int = 0
    traffic_split: float = 0.5
    started_at: datetime | None = None
    concluded_at: datetime | None = None
    winner_creative_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str | None = None
    actor_email: str = ""
    actor_role: str = ""
    action: str
    resource_type: str
    resource_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    ip_address: str = ""
    request_id: str = ""
    created_at: datetime | None = None


class SeedRequest(BaseModel):
    """Load the deterministic demo dataset."""

    force: bool = False


class SeedResult(BaseModel):
    created_admin: bool = False
    campaigns: int = 0
    creatives: int = 0
    daily_rows: int = 0
    skipped: bool = False


class SystemInfo(BaseModel):
    """Non-sensitive runtime configuration for the status page."""

    environment: str
    version: str
    data_mode: str
    llm_provider: str
    llm_model: str
    clickhouse_enabled: bool
    redis_enabled: bool
    database_dialect: str
    require_action_approval: bool
    orchestrator_mode: str
    cache_backend: str
    uptime_seconds: float
    # Tool-layer guardrails, so the status page states plainly whether an agent
    # is able to touch a live ad account right now.
    tools_enabled: bool = True
    tools_dry_run: bool = False
    tools_allow_agent_writes: bool = False


TokenResponse.model_rebuild()
