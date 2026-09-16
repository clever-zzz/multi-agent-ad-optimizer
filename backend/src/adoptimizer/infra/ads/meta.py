"""Meta Marketing API adapter (Graph API v21)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ...domain.enums import Platform
from .base import AdsPlatformClient, CreativeDraft, ExecutionResult
from .http_client import PlatformHTTPClient

logger = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
GRAPH_VERSION = "v21.0"


class MetaAdsClient(AdsPlatformClient):
    """Meta ads adapter using a long-lived system user access token."""

    platform = Platform.META

    def __init__(
        self,
        *,
        access_token: str,
        ad_account_id: str,
        app_secret: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_token = access_token
        self._ad_account_id = (
            ad_account_id
            if ad_account_id.startswith("act_") or not ad_account_id
            else "act_" + ad_account_id
        )
        self._app_secret = app_secret
        self.is_configured = bool(access_token and ad_account_id)
        self._http = (
            PlatformHTTPClient(GRAPH_BASE, timeout_seconds=30.0, max_retries=3, transport=transport)
            if self.is_configured
            else None
        )
        if not self.is_configured:
            logger.info("meta_ads_unconfigured")

    def _require(self) -> PlatformHTTPClient:
        if self._http is None:
            raise ExternalServiceError(
                "Meta Ads is not configured; set META_ACCESS_TOKEN and META_AD_ACCOUNT_ID"
            )
        return self._http

    def _auth_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"access_token": self._access_token}
        if extra:
            params.update(extra)
        return params

    async def update_campaign_budget(
        self, external_id: str, *, daily_budget: float
    ) -> ExecutionResult:
        client = self._require()
        payload = await client.request(
            "POST",
            "/" + GRAPH_VERSION + "/" + external_id,
            params=self._auth_params({"daily_budget": round(daily_budget * 100)}),
        )
        return ExecutionResult(
            success=bool(payload.get("success", True)),
            platform=self.platform,
            operation="update_budget",
            external_reference=external_id,
            detail={"daily_budget_cents": round(daily_budget * 100)},
        )

    async def _set_status(
        self, external_id: str, status: str, operation: str, reason: str
    ) -> ExecutionResult:
        client = self._require()
        payload = await client.request(
            "POST",
            "/" + GRAPH_VERSION + "/" + external_id,
            params=self._auth_params({"status": status}),
        )
        return ExecutionResult(
            success=bool(payload.get("success", True)),
            platform=self.platform,
            operation=operation,
            external_reference=external_id,
            detail={"status": status, "reason": reason},
        )

    async def pause_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_status(external_id, "PAUSED", "pause_campaign", reason)

    async def resume_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_status(external_id, "ACTIVE", "resume_campaign", reason)

    async def pause_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        # Meta pauses at the ad level; creatives have no independent status.
        return await self._set_status(external_id, "PAUSED", "pause_creative", reason)

    async def resume_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_status(external_id, "ACTIVE", "resume_creative", reason)

    async def create_creative(
        self, campaign_external_id: str, draft: CreativeDraft
    ) -> ExecutionResult:
        client = self._require()
        # ``object_story_spec`` must be a JSON-encoded string. Passing the dict
        # straight through would let httpx stringify it as a Python repr
        # (single quotes), which Meta rejects with a 400 rather than an error we
        # would recognise - so the encoding is explicit here.
        spec = {
            "page_id": campaign_external_id,
            "link_data": {
                "message": draft.description,
                "link": draft.asset_urls.get("link", ""),
                "call_to_action": {
                    "type": _meta_cta(draft.cta_text),
                    "value": {"link": draft.asset_urls.get("link", "")},
                },
            },
        }
        payload = await client.request(
            "POST",
            "/" + GRAPH_VERSION + "/" + self._ad_account_id + "/adcreatives",
            params=self._auth_params(
                {"name": draft.headline[:100], "object_story_spec": json.dumps(spec)}
            ),
        )
        return ExecutionResult(
            success="id" in payload,
            platform=self.platform,
            operation="create_creative",
            external_reference=str(payload.get("id", "")),
            detail={"headline": draft.headline},
        )

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        client = self._require()
        payload = await client.request(
            "GET",
            "/" + GRAPH_VERSION + "/" + external_id + "/insights",
            params=self._auth_params(
                {
                    "time_range": '{"since":"' + start_date + '","until":"' + end_date + '"}',
                    "time_increment": 1,
                    "fields": "impressions,clicks,actions,spend,date_start,date_stop",
                }
            ),
        )
        rows = [self._normalise_row(row) for row in payload.get("data", [])]
        return {"campaign_id": external_id, "source": "meta", "rows": rows}

    @staticmethod
    def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
        conversions = sum(
            int(float(action.get("value", 0) or 0))
            for action in row.get("actions", [])
            if action.get("action_type") in ("offsite_conversion.fb_pixel_purchase", "purchase")
        )
        return {
            "date": row.get("date_start"),
            "impressions": int(float(row.get("impressions", 0) or 0)),
            "clicks": int(float(row.get("clicks", 0) or 0)),
            "conversions": conversions,
            "cost": round(float(row.get("spend", 0) or 0), 4),
        }

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


def _meta_cta(cta_text: str) -> str:
    """Map free-form CTA copy onto Meta's fixed call-to-action enum."""
    mapping = {
        "buy": "SHOP_NOW",
        "shop": "SHOP_NOW",
        "learn": "LEARN_MORE",
        "sign": "SIGN_UP",
        "download": "MOBILE_APP",
        "contact": "CONTACT_US",
        "book": "BOOK_NOW",
    }
    lowered = cta_text.lower()
    for keyword, value in mapping.items():
        if keyword in lowered:
            return value
    return "LEARN_MORE"


def build_meta_client() -> MetaAdsClient:
    """Construct the adapter from environment configuration."""
    return MetaAdsClient(
        access_token=os.getenv("META_ACCESS_TOKEN", ""),
        ad_account_id=os.getenv("META_AD_ACCOUNT_ID", ""),
        app_secret=os.getenv("META_APP_SECRET", ""),
    )
