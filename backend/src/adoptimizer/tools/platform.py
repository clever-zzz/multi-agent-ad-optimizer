"""Tools bound to the ad-platform adapters.

One tool per capability the adapters expose, each with an explicit agent
allow-list. The set mirrors ``AdsPlatformClient`` exactly, so "what can this
system do" is a list you can read in one screen instead of something you have to
infer from call sites scattered across services.

In ``DATA_MODE=mock`` the registry routes every platform to the mock adapter, so
these tools can be exercised end to end without credentials. Each result reports
``served_by`` so a mock answer never masquerades as a real one.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import Field

from ..domain.enums import AgentName, Platform
from ..infra.ads.base import AdsPlatformClient, CreativeDraft, ExecutionResult
from ..infra.ads.registry import PlatformRegistry
from .spec import ToolArguments, ToolRequest, ToolSpec

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
REASON_MAX = 200
EXTERNAL_ID_MAX = 120


class CampaignReportArgs(ToolArguments):
    """Read a delivery report straight off the network."""

    platform: Platform
    campaign_external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX)
    start_date: str = Field(pattern=DATE_PATTERN)
    end_date: str = Field(pattern=DATE_PATTERN)


class SetDailyBudgetArgs(ToolArguments):
    """Change how much a campaign may spend per day."""

    platform: Platform
    campaign_external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX)
    daily_budget: float = Field(gt=0, le=10_000_000)
    reason: str = Field(min_length=1, max_length=REASON_MAX)


class CampaignStatusArgs(ToolArguments):
    """Pause or resume a whole campaign."""

    platform: Platform
    campaign_external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX)
    reason: str = Field(min_length=1, max_length=REASON_MAX)


class CreativeStatusArgs(ToolArguments):
    """Pause or resume one creative."""

    platform: Platform
    creative_external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX)
    reason: str = Field(min_length=1, max_length=REASON_MAX)


class CreateCreativeArgs(ToolArguments):
    """Publish a new creative under an existing campaign."""

    platform: Platform
    campaign_external_id: str = Field(min_length=1, max_length=EXTERNAL_ID_MAX)
    headline: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    cta_text: str = Field(default="Learn More", max_length=40)
    creative_type: str = Field(default="text", max_length=30)


class PlatformTools:
    """Binds the platform tool set to one live adapter registry."""

    def __init__(self, platforms: PlatformRegistry) -> None:
        self._platforms = platforms

    def specs(self) -> list[ToolSpec]:
        """The catalogue, in the order an operator would want to read it."""
        return [
            ToolSpec(
                name="platform.campaign_report",
                description=(
                    "Fetch a delivery report for one campaign from its ad network. "
                    "Read-only and safe to call every iteration."
                ),
                parameters=CampaignReportArgs,
                handler=self.campaign_report,
                read_only=True,
                touches_platform=True,
            ),
            ToolSpec(
                name="platform.set_daily_budget",
                description=(
                    "Set a campaign's daily budget on the ad network. Changes real "
                    "spend, so an agent calling it receives a dry-run preflight."
                ),
                parameters=SetDailyBudgetArgs,
                handler=self.set_daily_budget,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="platform.pause_campaign",
                description="Stop a campaign from delivering. Changes real spend.",
                parameters=CampaignStatusArgs,
                handler=self.pause_campaign,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="platform.resume_campaign",
                description="Resume a paused campaign. Changes real spend.",
                parameters=CampaignStatusArgs,
                handler=self.resume_campaign,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="platform.pause_creative",
                description="Stop one creative from serving.",
                parameters=CreativeStatusArgs,
                handler=self.pause_creative,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.CREATIVE, AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="platform.resume_creative",
                description="Put a paused creative back into rotation.",
                parameters=CreativeStatusArgs,
                handler=self.resume_creative,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.CREATIVE, AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="platform.create_creative",
                description="Publish a new creative under an existing campaign.",
                parameters=CreateCreativeArgs,
                handler=self.create_creative,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.CREATIVE}),
            ),
        ]

    def _client(self, platform: Platform) -> AdsPlatformClient:
        return self._platforms.for_platform(platform)

    async def campaign_report(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CampaignReportArgs, args)
        client = self._client(params.platform)
        report = await client.fetch_report(
            params.campaign_external_id,
            start_date=params.start_date,
            end_date=params.end_date,
        )
        payload = dict(report)
        payload["served_by"] = client.platform.value
        payload["configured"] = bool(client.is_configured)
        payload["requested_by"] = _caller(request)
        return payload

    async def set_daily_budget(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(SetDailyBudgetArgs, args)
        result = await self._client(params.platform).update_campaign_budget(
            params.campaign_external_id, daily_budget=round(params.daily_budget, 2)
        )
        return _execution(result, request)

    async def pause_campaign(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CampaignStatusArgs, args)
        result = await self._client(params.platform).pause_campaign(
            params.campaign_external_id, reason=params.reason
        )
        return _execution(result, request)

    async def resume_campaign(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CampaignStatusArgs, args)
        result = await self._client(params.platform).resume_campaign(
            params.campaign_external_id, reason=params.reason
        )
        return _execution(result, request)

    async def pause_creative(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CreativeStatusArgs, args)
        result = await self._client(params.platform).pause_creative(
            params.creative_external_id, reason=params.reason
        )
        return _execution(result, request)

    async def resume_creative(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CreativeStatusArgs, args)
        result = await self._client(params.platform).resume_creative(
            params.creative_external_id, reason=params.reason
        )
        return _execution(result, request)

    async def create_creative(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        params = cast(CreateCreativeArgs, args)
        draft = CreativeDraft(
            headline=params.headline,
            description=params.description,
            cta_text=params.cta_text,
            creative_type=params.creative_type,
        )
        result = await self._client(params.platform).create_creative(
            params.campaign_external_id, draft
        )
        return _execution(result, request)


def _caller(request: ToolRequest) -> str:
    return request.agent.value if request.agent is not None else request.actor


def _execution(result: ExecutionResult, request: ToolRequest) -> dict[str, Any]:
    """Normalise an adapter result, keeping the caller visible in the record."""
    payload = result.to_dict()
    payload["requested_by"] = _caller(request)
    return payload


def build_platform_tools(platforms: PlatformRegistry) -> list[ToolSpec]:
    """Create the platform tool set bound to a live registry."""
    return PlatformTools(platforms).specs()
