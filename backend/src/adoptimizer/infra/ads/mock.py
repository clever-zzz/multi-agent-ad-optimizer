"""Deterministic in-memory platform adapter for development and tests.

Unlike the demo mock, every mutation is recorded so tests can assert on what
would have been sent, and unconfigured operations are explicit rather than
pretending to talk to a real network.
"""

from __future__ import annotations

from typing import Any

from ...core.logging import get_logger
from ...domain.enums import Platform
from .base import AdsPlatformClient, CreativeDraft, ExecutionResult

logger = get_logger(__name__)


class MockAdsClient(AdsPlatformClient):
    """Records mutations and returns plausible platform identifiers."""

    def __init__(
        self, platform: Platform = Platform.MOCK, *, fail_operations: set[str] | None = None
    ) -> None:
        self.platform = platform
        self.is_configured = True
        self.calls: list[dict[str, Any]] = []
        self._fail_operations = fail_operations or set()
        self._sequence = 0

    def _record(self, operation: str, **detail: Any) -> ExecutionResult:
        self._sequence += 1
        self.calls.append({"operation": operation, "sequence": self._sequence, **detail})
        reference = "mock_" + operation + "_" + str(self._sequence)

        if operation in self._fail_operations:
            logger.warning("mock_operation_failed", operation=operation)
            return ExecutionResult(
                success=False,
                platform=self.platform,
                operation=operation,
                error="Simulated platform failure for " + operation,
                detail=detail,
            )

        logger.info("mock_ads_operation", operation=operation, detail=detail)
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation=operation,
            external_reference=reference,
            detail=detail,
        )

    async def update_campaign_budget(
        self, external_id: str, *, daily_budget: float
    ) -> ExecutionResult:
        return self._record(
            "update_budget", external_id=external_id, daily_budget=round(daily_budget, 2)
        )

    async def pause_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return self._record("pause_campaign", external_id=external_id, reason=reason)

    async def resume_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return self._record("resume_campaign", external_id=external_id, reason=reason)

    async def pause_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return self._record("pause_creative", external_id=external_id, reason=reason)

    async def resume_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return self._record("resume_creative", external_id=external_id, reason=reason)

    async def create_creative(
        self, campaign_external_id: str, draft: CreativeDraft
    ) -> ExecutionResult:
        return self._record(
            "create_creative",
            campaign_external_id=campaign_external_id,
            headline=draft.headline,
            cta_text=draft.cta_text,
        )

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        return {
            "campaign_id": external_id,
            "start_date": start_date,
            "end_date": end_date,
            "source": "mock",
            "rows": [],
        }

    async def close(self) -> None:
        return None
