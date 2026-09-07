"""TikTok Marketing API adapter (v1.3)."""

from __future__ import annotations

import os
from typing import Any

from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ...domain.enums import Platform
from .base import AdsPlatformClient, CreativeDraft, ExecutionResult
from .http_client import PlatformHTTPClient

logger = get_logger(__name__)

TIKTOK_BASE = "https://business-api.tiktok.com"
API_VERSION = "open_api/v1.3"


class TikTokAdsClient(AdsPlatformClient):
    """TikTok ads adapter using an app access token."""

    platform = Platform.TIKTOK

    def __init__(self, *, access_token: str, advertiser_id: str) -> None:
        self._access_token = access_token
        self._advertiser_id = advertiser_id
        self.is_configured = bool(access_token and advertiser_id)
        self._http = (
            PlatformHTTPClient(TIKTOK_BASE, timeout_seconds=30.0, max_retries=3)
            if self.is_configured
            else None
        )
        if not self.is_configured:
            logger.info("tiktok_ads_unconfigured")

    def _require(self) -> PlatformHTTPClient:
        if self._http is None:
            raise ExternalServiceError(
                "TikTok Ads is not configured; set TIKTOK_ACCESS_TOKEN and TIKTOK_ADVERTISER_ID"
            )
        return self._http

    def _headers(self) -> dict[str, Any]:
        return {"Access-Token": self._access_token}

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._require()
        payload = await client.request(
            "POST",
            "/" + API_VERSION + "/" + path,
            json_body={"advertiser_id": self._advertiser_id, **body},
        )
        code = payload.get("code")
        if code not in (0, None):
            raise ExternalServiceError(
                "TikTok API error " + str(code) + ": " + str(payload.get("message", ""))
            )
        return payload.get("data", {}) or {}

    async def update_campaign_budget(
        self, external_id: str, *, daily_budget: float
    ) -> ExecutionResult:
        data = await self._post(
            "campaign/update/",
            {
                "campaign_id": external_id,
                "budget": round(daily_budget, 2),
                "budget_mode": "BUDGET_MODE_DAY",
            },
        )
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation="update_budget",
            external_reference=str(data.get("campaign_id", external_id)),
            detail={"daily_budget": round(daily_budget, 2)},
        )

    async def _set_status(
        self, external_id: str, operation: str, status: str, reason: str
    ) -> ExecutionResult:
        await self._post(
            "campaign/status/update/",
            {"campaign_ids": [external_id], "operation": status},
        )
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation=operation,
            external_reference=external_id,
            detail={"status": status, "reason": reason},
        )

    async def pause_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_status(external_id, "pause_campaign", "DISABLE", reason)

    async def resume_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_status(external_id, "resume_campaign", "ENABLE", reason)

    async def _set_ad_status(
        self, external_id: str, operation: str, status: str, reason: str
    ) -> ExecutionResult:
        await self._post("ad/status/update/", {"ad_ids": [external_id], "operation": status})
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation=operation,
            external_reference=external_id,
            detail={"status": status, "reason": reason},
        )

    async def pause_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_ad_status(external_id, "pause_creative", "DISABLE", reason)

    async def resume_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_ad_status(external_id, "resume_creative", "ENABLE", reason)

    async def create_creative(
        self, campaign_external_id: str, draft: CreativeDraft
    ) -> ExecutionResult:
        raise ExternalServiceError(
            "TikTok creative creation needs an uploaded video asset id; run the "
            "asset upload flow before creating the creative",
            detail={"campaign_external_id": campaign_external_id},
        )

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        client = self._require()
        payload = await client.request(
            "GET",
            "/" + API_VERSION + "/report/integrated/get/",
            params={
                "advertiser_id": self._advertiser_id,
                "report_type": "BASIC",
                "dimensions": '["stat_time_day"]',
                "data_level": "AUCTION_CAMPAIGN",
                "filters": '[{"field":"campaign_id","operator":"=","values":["'
                + external_id
                + '"]}]',
                "start_date": start_date,
                "end_date": end_date,
                "Access-Token": self._access_token,
            },
        )
        rows = [self._normalise_row(row) for row in payload.get("data", {}).get("list", [])]
        return {"campaign_id": external_id, "source": "tiktok", "rows": rows}

    @staticmethod
    def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
        metrics = row.get("metrics", {})
        return {
            "date": (row.get("dimensions") or {}).get("stat_time_day"),
            "impressions": int(float(metrics.get("impressions", 0) or 0)),
            "clicks": int(float(metrics.get("clicks", 0) or 0)),
            "conversions": int(float(metrics.get("conversion", 0) or 0)),
            "cost": round(float(metrics.get("spend", 0) or 0), 4),
        }

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


def build_tiktok_client() -> TikTokAdsClient:
    """Construct the adapter from environment configuration."""
    return TikTokAdsClient(
        access_token=os.getenv("TIKTOK_ACCESS_TOKEN", ""),
        advertiser_id=os.getenv("TIKTOK_ADVERTISER_ID", ""),
    )
