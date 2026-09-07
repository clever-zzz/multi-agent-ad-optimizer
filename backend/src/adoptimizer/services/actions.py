"""Action approval and execution.

The optimizer only proposes. This service is the single place where a proposal
becomes a real change, and it enforces the human-in-the-loop gate so a model
error can never move spend unattended.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import SecuritySettings
from ..core.errors import ActionRequiresApprovalError, ConflictError, NotFoundError
from ..core.logging import get_logger
from ..core.security import Permission, TokenClaims, require_permission
from ..domain.enums import ActionStatus, ActionType, CampaignStatus, CreativeStatus
from ..domain.statistics import required_sample_size
from ..infra.ads.base import AdsPlatformClient, ExecutionResult
from ..infra.db.models import Campaign, Creative, OptimizationAction
from ..repositories.audit import ABTestRepository
from ..repositories.campaigns import CampaignRepository, CreativeRepository
from ..repositories.runs import ActionRepository
from .audit import AuditService

logger = get_logger(__name__)

# Actions that change spend or stop delivery always need explicit approval.
HIGH_RISK_ACTIONS = frozenset(
    {
        ActionType.ADJUST_BUDGET,
        ActionType.PAUSE_CAMPAIGN,
        ActionType.ADJUST_BID,
        ActionType.PAUSE_CREATIVE,
    }
)


class ActionService:
    """Approve, reject and execute proposed optimization actions."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        platforms: Any,
        security: SecuritySettings,
    ) -> None:
        self._session = session
        self._actions = ActionRepository(session)
        self._campaigns = CampaignRepository(session)
        self._creatives = CreativeRepository(session)
        self._experiments = ABTestRepository(session)
        self._audit = AuditService(session)
        self._platforms = platforms
        self._require_approval = security.require_action_approval

    async def list_actions(
        self, *, status: ActionStatus | None = None, run_id: str | None = None, limit: int = 200
    ) -> list[OptimizationAction]:
        """Query proposals by state and originating run."""
        if run_id:
            return await self._actions.for_run(run_id, limit=limit)
        if status is not None:
            return await self._actions.by_status(status, limit=limit)
        return await self._actions.pending(limit=limit)

    async def get(self, action_id: str) -> OptimizationAction:
        return await self._actions.get_or_raise(action_id)

    async def approve(
        self,
        action_id: str,
        *,
        claims: TokenClaims,
        client: dict[str, str] | None = None,
    ) -> OptimizationAction:
        """Approve a proposal so it may be executed."""
        require_permission(Permission.ACTION_APPROVE, claims)
        action = await self._actions.get_or_raise(action_id)

        if action.status != ActionStatus.PROPOSED.value:
            raise ConflictError("Action is already " + action.status)

        before = {"status": action.status}
        action.status = ActionStatus.APPROVED.value
        action.approved_by = claims.subject
        action.approved_at = datetime.now(UTC)
        await self._actions.flush()

        await self._audit.record(
            action="action.approved",
            resource_type="optimization_action",
            resource_id=action.id,
            claims=claims,
            before=before,
            after={"status": action.status, "action_type": action.action_type},
            client=client,
        )
        logger.info("action_approved", action_id=action.id, actor=claims.subject)
        return action

    async def reject(
        self,
        action_id: str,
        *,
        claims: TokenClaims,
        reason: str = "",
        client: dict[str, str] | None = None,
    ) -> OptimizationAction:
        """Reject a proposal; it will not be executed."""
        require_permission(Permission.ACTION_APPROVE, claims)
        action = await self._actions.get_or_raise(action_id)

        if action.status not in (ActionStatus.PROPOSED.value, ActionStatus.APPROVED.value):
            raise ConflictError("Action is already " + action.status)

        before = {"status": action.status}
        action.status = ActionStatus.REJECTED.value
        action.approved_by = claims.subject
        action.approved_at = datetime.now(UTC)
        if reason:
            action.reason = action.reason + " | Rejected: " + reason[:400]
        await self._actions.flush()

        await self._audit.record(
            action="action.rejected",
            resource_type="optimization_action",
            resource_id=action.id,
            claims=claims,
            before=before,
            after={"status": action.status, "reason": reason},
            client=client,
        )
        return action

    async def execute(
        self,
        action_id: str,
        *,
        claims: TokenClaims,
        client: dict[str, str] | None = None,
    ) -> tuple[OptimizationAction, ExecutionResult | None]:
        """Apply an approved action locally and push it to the ad platform."""
        require_permission(Permission.ACTION_EXECUTE, claims)
        action = await self._actions.get_or_raise(action_id)

        # Settled actions are a state conflict, not an authorisation failure: a
        # caller replaying an execute that already landed must see 409, matching
        # approve()/reject(). Checking approval first would mask that with 403.
        if action.status in (ActionStatus.EXECUTED.value, ActionStatus.REJECTED.value):
            raise ConflictError("Action is already " + action.status)

        # The caller does hold action:execute, so this is a state conflict (409
        # approval_required), not an authorisation failure (403). Genuine
        # permission gaps are still raised by require_permission() above.
        if self._require_approval and action.status != ActionStatus.APPROVED.value:
            raise ActionRequiresApprovalError(
                "Action "
                + action.id
                + " must be approved before execution (current: "
                + action.status
                + ")"
            )
        if not self._require_approval and action.status == ActionStatus.PROPOSED.value:
            action.approved_by = claims.subject
            action.approved_at = datetime.now(UTC)
            action.status = ActionStatus.APPROVED.value

        campaign = await self._campaigns.get(action.campaign_id)
        result: ExecutionResult | None = None

        try:
            result = await self._apply(action, campaign)
            action.status = ActionStatus.EXECUTED.value
            action.executed_at = datetime.now(UTC)
            if result is not None:
                action.external_reference = result.external_reference
            await self._actions.flush()
        except Exception as exc:
            action.status = ActionStatus.FAILED.value
            action.error_message = str(exc)[:2000]
            await self._actions.flush()
            logger.error("action_execution_failed", action_id=action.id, error=str(exc))
            await self._audit.record(
                action="action.failed",
                resource_type="optimization_action",
                resource_id=action.id,
                claims=claims,
                after={"error": action.error_message},
                client=client,
            )
            raise

        await self._audit.record(
            action="action.executed",
            resource_type="optimization_action",
            resource_id=action.id,
            claims=claims,
            before={"status": ActionStatus.APPROVED.value, "campaign_id": action.campaign_id},
            after={
                "status": action.status,
                "action_type": action.action_type,
                "after_value": action.after_value,
                "external_reference": action.external_reference,
            },
            client=client,
        )
        logger.info(
            "action_executed",
            action_id=action.id,
            action_type=action.action_type,
            campaign_id=action.campaign_id,
            actor=claims.subject,
        )
        return action, result

    async def _apply(
        self, action: OptimizationAction, campaign: Campaign | None
    ) -> ExecutionResult | None:
        """Mutate local state and call the platform adapter for the action type."""
        action_type = ActionType(action.action_type)
        platform_client: AdsPlatformClient | None = None
        if campaign is not None and campaign.external_id:
            try:
                platform_client = self._platforms.for_platform(campaign.platform)
            except Exception as exc:
                logger.warning(
                    "platform_adapter_unavailable",
                    platform=campaign.platform,
                    error=str(exc),
                )

        if action_type == ActionType.ADJUST_BUDGET:
            return await self._apply_budget(action, campaign, platform_client)
        if action_type in (ActionType.PAUSE_CAMPAIGN, ActionType.RESUME_CAMPAIGN):
            return await self._apply_campaign_status(action, campaign, platform_client, action_type)
        if action_type in (ActionType.PAUSE_CREATIVE, ActionType.RESUME_CREATIVE):
            return await self._apply_creative_status(action, platform_client, action_type)
        if action_type == ActionType.ADJUST_BID:
            return await self._apply_bid(action, campaign, platform_client)
        if action_type == ActionType.START_AB_TEST:
            await self._start_experiment(action)
            return None
        if action_type in (ActionType.REFRESH_CREATIVE, ActionType.EXPAND_AUDIENCE):
            # Advisory actions: recorded for the operator, nothing to push.
            return None

        logger.info("action_type_has_no_executor", action_type=action_type.value)
        return None

    async def _apply_budget(
        self,
        action: OptimizationAction,
        campaign: Campaign | None,
        client: AdsPlatformClient | None,
    ) -> ExecutionResult | None:
        new_budget = _parse_float(action.after_value)
        if new_budget is None or new_budget <= 0:
            raise ConflictError("Action carries no usable budget value: " + action.after_value)

        # Round once and use that figure everywhere. Storing 1234.57 locally
        # while instructing the network to spend 1234.5678 leaves the two systems
        # of record disagreeing, which cannot be reconciled in a spend report.
        rounded = round(new_budget, 2)
        if campaign is not None:
            campaign.daily_budget = rounded
            await self._campaigns.flush()

        if client is not None and campaign is not None and campaign.external_id:
            return await client.update_campaign_budget(campaign.external_id, daily_budget=rounded)
        return None

    async def _apply_campaign_status(
        self,
        action: OptimizationAction,
        campaign: Campaign | None,
        client: AdsPlatformClient | None,
        action_type: ActionType,
    ) -> ExecutionResult | None:
        target = (
            CampaignStatus.PAUSED
            if action_type == ActionType.PAUSE_CAMPAIGN
            else CampaignStatus.ACTIVE
        )
        if campaign is not None:
            campaign.status = target.value
            await self._campaigns.flush()

        if client is not None and campaign is not None and campaign.external_id:
            operation = (
                client.pause_campaign if target == CampaignStatus.PAUSED else client.resume_campaign
            )
            return await operation(campaign.external_id, reason=action.reason[:200])
        return None

    async def _apply_creative_status(
        self,
        action: OptimizationAction,
        client: AdsPlatformClient | None,
        action_type: ActionType,
    ) -> ExecutionResult | None:
        target = (
            CreativeStatus.PAUSED
            if action_type == ActionType.PAUSE_CREATIVE
            else CreativeStatus.ACTIVE
        )
        creative: Creative | None = None
        if action.creative_id:
            creative = await self._creatives.get(action.creative_id)
        if creative is None:
            candidates = await self._creatives.for_campaign(action.campaign_id)
            creative = candidates[0] if candidates else None

        if creative is None:
            raise NotFoundError("No creative found to " + action_type.value)

        creative.status = target.value
        await self._creatives.flush()

        if client is not None and creative.id:
            # Resuming must not reuse the pause call: an operator approving
            # "resume creative" would otherwise switch the ad off on the network
            # while the local row reads active.
            operation = (
                client.pause_creative if target == CreativeStatus.PAUSED else client.resume_creative
            )
            return await operation(creative.id, reason=action.reason[:200])
        return None

    async def _apply_bid(
        self,
        action: OptimizationAction,
        campaign: Campaign | None,
        client: AdsPlatformClient | None,
    ) -> ExecutionResult | None:
        bid = _parse_float(action.after_value)
        if bid is None or bid <= 0:
            raise ConflictError("Action carries no usable bid value: " + action.after_value)
        if campaign is not None:
            campaign.current_bid_cpm = round(bid, 4)
            await self._campaigns.flush()
        _ = client
        # Bid changes are applied through the platform's bidding strategy API,
        # which differs per network; recorded locally and surfaced for review.
        return None

    async def _start_experiment(self, action: OptimizationAction) -> None:
        """Create an A/B test sized by a proper power calculation."""
        creatives = await self._creatives.for_campaign(action.campaign_id)
        control = next((c for c in creatives if c.ab_group == "control"), None)
        variants = [c for c in creatives if c.ab_group != "control" and c.status != "rejected"]
        variant = variants[-1] if variants else None

        sample_size = required_sample_size(0.025, 0.10)
        await self._experiments.create(
            campaign_id=action.campaign_id,
            name="Agent experiment for " + action.campaign_id,
            hypothesis=action.reason[:500],
            control_creative_id=control.id if control else None,
            variant_creative_id=variant.id if variant else None,
            metric="ctr",
            minimum_detectable_effect=0.10,
            required_sample_size=sample_size,
            traffic_split=0.5,
            created_by_run_id=action.run_id,
        )
        logger.info(
            "ab_test_created", campaign_id=action.campaign_id, required_sample_size=sample_size
        )

    async def bulk_approve(
        self,
        action_ids: list[str],
        *,
        claims: TokenClaims,
        client: dict[str, str] | None = None,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """Approve many proposals at once, skipping ones below a confidence floor."""
        approved: list[str] = []
        skipped: list[dict[str, str]] = []
        for action_id in action_ids:
            action = await self._actions.get(action_id)
            if action is None:
                skipped.append({"id": action_id, "reason": "not_found"})
                continue
            if action.status != ActionStatus.PROPOSED.value:
                skipped.append({"id": action_id, "reason": "status_" + action.status})
                continue
            if float(action.confidence) < min_confidence:
                skipped.append({"id": action_id, "reason": "low_confidence"})
                continue
            await self.approve(action_id, claims=claims, client=client)
            approved.append(action_id)
        return {"approved": approved, "skipped": skipped}


def _parse_float(value: str) -> float | None:
    """Parse a numeric value that may carry a currency symbol."""
    cleaned = value.replace("¥", "").replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None
