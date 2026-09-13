"""Action approval and execution.

The optimizer only proposes. This service is the single place where a proposal
becomes a real change, and it enforces the human-in-the-loop gate so a model
error can never move spend unattended.

Once approved, the change reaches the network through the tool executor rather
than through a platform client held by this service. That is not ceremony: the
executor is what stamps the call with an audit row, an idempotency key and the
deployment-wide dry-run switch, so "an agent moved money" is always a sentence
somebody can read back instead of a stack trace nobody logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import SecuritySettings
from ..core.errors import (
    ActionRequiresApprovalError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationFailure,
)
from ..core.logging import get_logger
from ..core.security import Permission, TokenClaims, require_permission
from ..domain.enums import ActionStatus, ActionType, CampaignStatus, CreativeStatus, Platform
from ..domain.statistics import required_sample_size
from ..infra.ads.base import AdsPlatformClient, ExecutionResult
from ..infra.db.models import Campaign, Creative, OptimizationAction
from ..repositories.audit import ABTestRepository
from ..repositories.campaigns import CampaignRepository, CreativeRepository
from ..repositories.runs import ActionRepository
from ..tools.executor import ToolExecutor
from ..tools.spec import ToolOutcome, ToolRequest, ToolResult
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

# Mirrors REASON_MAX in the tool schema. The executor validates a rationale as
# 1-200 characters, so truncating here keeps a verbose proposal from being
# refused as malformed on its way to the network.
REASON_MAX = 200


class ActionService:
    """Approve, reject and execute proposed optimization actions."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        platforms: Any,
        security: SecuritySettings,
        tools: ToolExecutor | None = None,
    ) -> None:
        self._session = session
        self._actions = ActionRepository(session)
        self._campaigns = CampaignRepository(session)
        self._creatives = CreativeRepository(session)
        self._experiments = ABTestRepository(session)
        self._audit = AuditService(session)
        self._platforms = platforms
        self._tools = tools
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
                # A dry-run execution still settles the action locally, so the
                # audit row has to say whether the network was really touched.
                "dry_run": result.dry_run if result is not None else None,
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
        # Resolved apart from the adapter on purpose: the tool layer owns its own
        # registry, so a campaign whose adapter lookup failed here can still be
        # routed through the executor and get an honest failure back from it.
        platform = _platform_of(campaign)
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
            return await self._apply_budget(action, campaign, platform_client, platform)
        if action_type in (ActionType.PAUSE_CAMPAIGN, ActionType.RESUME_CAMPAIGN):
            return await self._apply_campaign_status(
                action, campaign, platform_client, action_type, platform
            )
        if action_type in (ActionType.PAUSE_CREATIVE, ActionType.RESUME_CREATIVE):
            return await self._apply_creative_status(action, platform_client, action_type, platform)
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

    async def _push(
        self,
        action: OptimizationAction,
        *,
        tool: str,
        operation: str,
        platform: Platform | None,
        arguments: dict[str, Any],
    ) -> ExecutionResult | None:
        """Send an approved change to the network through the tool executor.

        agent=None is the load-bearing argument: the executor dry-runs every
        agent-initiated write, but this call already carries a human approval, so
        it is allowed to be real. Whether it actually is then depends on
        TOOLS__DRY_RUN alone, which keeps one switch in charge of "does this
        deployment touch money". Returns None when the tool layer is off or the
        campaign was never synced, so the caller falls back to the adapter.
        """
        executor = self._tools
        if executor is None or not executor.enabled or platform is None:
            return None

        call_arguments = dict(arguments)
        call_arguments["platform"] = platform.value
        result = await executor.call(
            ToolRequest(
                tool=tool,
                arguments=call_arguments,
                agent=None,
                run_id=str(action.run_id or ""),
                actor=str(action.approved_by or "system"),
                # Keyed on the action rather than the attempt, so a retried
                # execute replays the recorded result instead of charging the
                # ad network twice for one approval.
                idempotency_key="action:" + str(action.id),
            )
        )
        if result.outcome is ToolOutcome.VALIDATION_FAILED:
            raise ValidationFailure(
                "Action "
                + str(action.id)
                + " could not be sent to "
                + tool
                + ": "
                + str(result.error or "rejected by the tool schema"),
                detail={"tool": tool, "action_id": action.id},
            )
        if result.outcome is ToolOutcome.FAILED or result.refused:
            # Parity with the direct adapter path, where a platform error
            # propagates and the action lands as failed rather than executed.
            raise ExternalServiceError(
                tool
                + " did not run for action "
                + str(action.id)
                + " ("
                + result.outcome.value
                + "): "
                + str(result.error or "no reason reported"),
                detail={"tool": tool, "outcome": result.outcome.value, "action_id": action.id},
            )
        return _execution_from_tool(result, platform=platform, operation=operation)

    async def _apply_budget(
        self,
        action: OptimizationAction,
        campaign: Campaign | None,
        client: AdsPlatformClient | None,
        platform: Platform | None,
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

        if campaign is None or not campaign.external_id:
            return None
        pushed = await self._push(
            action,
            tool="platform.set_daily_budget",
            operation="update_budget",
            platform=platform,
            arguments={
                "campaign_external_id": campaign.external_id,
                "daily_budget": rounded,
                "reason": _reason(action),
            },
        )
        if pushed is not None:
            return pushed
        if client is not None:
            return await client.update_campaign_budget(campaign.external_id, daily_budget=rounded)
        return None

    async def _apply_campaign_status(
        self,
        action: OptimizationAction,
        campaign: Campaign | None,
        client: AdsPlatformClient | None,
        action_type: ActionType,
        platform: Platform | None,
    ) -> ExecutionResult | None:
        target = (
            CampaignStatus.PAUSED
            if action_type == ActionType.PAUSE_CAMPAIGN
            else CampaignStatus.ACTIVE
        )
        if campaign is not None:
            campaign.status = target.value
            await self._campaigns.flush()

        if campaign is None or not campaign.external_id:
            return None
        pausing = target == CampaignStatus.PAUSED
        pushed = await self._push(
            action,
            tool="platform.pause_campaign" if pausing else "platform.resume_campaign",
            operation="pause_campaign" if pausing else "resume_campaign",
            platform=platform,
            arguments={"campaign_external_id": campaign.external_id, "reason": _reason(action)},
        )
        if pushed is not None:
            return pushed
        if client is not None:
            call = client.pause_campaign if pausing else client.resume_campaign
            return await call(campaign.external_id, reason=action.reason[:200])
        return None

    async def _apply_creative_status(
        self,
        action: OptimizationAction,
        client: AdsPlatformClient | None,
        action_type: ActionType,
        platform: Platform | None,
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

        if not creative.id:
            return None
        # Resuming must not reuse the pause call: an operator approving "resume
        # creative" would otherwise switch the ad off on the network while the
        # local row reads active.
        pausing = target == CreativeStatus.PAUSED
        pushed = await self._push(
            action,
            tool="platform.pause_creative" if pausing else "platform.resume_creative",
            operation="pause_creative" if pausing else "resume_creative",
            platform=platform,
            arguments={"creative_external_id": creative.id, "reason": _reason(action)},
        )
        if pushed is not None:
            return pushed
        if client is not None:
            call = client.pause_creative if pausing else client.resume_creative
            return await call(creative.id, reason=action.reason[:200])
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


def _reason(action: OptimizationAction) -> str:
    """A rationale the tool schema accepts: never empty, never over-long."""
    return str(action.reason or "").strip()[:REASON_MAX] or str(action.action_type)


def _platform_of(campaign: Campaign | None) -> Platform | None:
    """The network a campaign lives on, or None when it was never synced to one."""
    if campaign is None or not campaign.external_id:
        return None
    try:
        return Platform(str(campaign.platform))
    except ValueError:
        return None


def _execution_from_tool(
    result: ToolResult, *, platform: Platform, operation: str
) -> ExecutionResult:
    """Rebuild the adapter result from what the executor returned.

    A successful call carries the adapter payload verbatim, so this is a
    re-hydration rather than a translation. A dry run carries no adapter payload
    at all - the executor never reached the network - so the rehearsal record
    becomes the detail and dry_run stays true, which is what stops an approval
    screen from claiming the network accepted something it was never asked about.
    """
    if result.dry_run:
        return ExecutionResult(
            success=True,
            platform=platform,
            operation=operation,
            detail=dict(result.data),
            dry_run=True,
        )
    payload: dict[str, Any] = dict(result.data)
    detail = payload.get("detail")
    reference = payload.get("external_reference")
    error = payload.get("error")
    return ExecutionResult(
        success=bool(payload.get("success", True)),
        platform=platform,
        operation=str(payload.get("operation") or operation),
        external_reference=str(reference) if reference else None,
        detail=dict(detail) if isinstance(detail, dict) else {},
        error=str(error) if error else None,
        dry_run=bool(payload.get("dry_run", False)),
    )


def _parse_float(value: str) -> float | None:
    """Parse a numeric value that may carry a currency symbol."""
    cleaned = value.replace("¥", "").replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None
