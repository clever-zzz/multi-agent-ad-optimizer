"""Platform client protocol and shared value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ...domain.enums import Platform


@dataclass(frozen=True, slots=True)
class CampaignDraft:
    """Fields required to create or update a campaign on a platform."""

    name: str
    daily_budget: float
    total_budget: float = 0.0
    target_cpa: float = 100.0
    objective: str = "conversions"
    start_date: str | None = None
    end_date: str | None = None
    targeting: dict[str, Any] = field(default_factory=dict)
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreativeDraft:
    """Fields required to create a creative on a platform."""

    headline: str
    description: str
    cta_text: str = "Learn More"
    creative_type: str = "text"
    asset_urls: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of a mutation against an ad platform."""

    success: bool
    platform: Platform
    operation: str
    external_reference: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "platform": self.platform.value,
            "operation": self.operation,
            "external_reference": self.external_reference,
            "detail": self.detail,
            "error": self.error,
            "executed_at": self.executed_at.isoformat(),
            "dry_run": self.dry_run,
        }


class AdsPlatformClient(Protocol):
    """Operations the optimizer may perform against an ad network."""

    platform: Platform
    is_configured: bool

    async def update_campaign_budget(
        self, external_id: str, *, daily_budget: float
    ) -> ExecutionResult: ...

    async def pause_campaign(self, external_id: str, *, reason: str) -> ExecutionResult: ...

    async def resume_campaign(self, external_id: str, *, reason: str) -> ExecutionResult: ...

    async def pause_creative(self, external_id: str, *, reason: str) -> ExecutionResult: ...

    async def resume_creative(self, external_id: str, *, reason: str) -> ExecutionResult: ...

    async def create_creative(
        self, campaign_external_id: str, draft: CreativeDraft
    ) -> ExecutionResult: ...

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...
