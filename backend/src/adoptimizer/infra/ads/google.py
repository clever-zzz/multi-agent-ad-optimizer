"""Google Ads adapter using the REST API.

Uses the v18 REST surface with an OAuth2 refresh-token flow so no heavyweight
SDK is required. Credentials come from configuration; when they are absent the
adapter reports itself unconfigured instead of faking success.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ...domain.enums import Platform
from .base import AdsPlatformClient, CreativeDraft, ExecutionResult
from .http_client import PlatformHTTPClient

logger = get_logger(__name__)

GOOGLE_ADS_API_BASE = "https://googleads.googleapis.com"
GOOGLE_ADS_VERSION = "v18"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # endpoint, not a secret


class GoogleAdsCredentials:
    """Holds and refreshes an OAuth2 access token."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        developer_token: str,
        customer_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.developer_token = developer_token
        self.customer_id = customer_id.replace("-", "")
        # Shared with the REST client so one injected transport covers both the
        # OAuth refresh call and every subsequent API call.
        self.transport = transport
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @property
    def is_complete(self) -> bool:
        return all(
            [
                self.client_id,
                self.client_secret,
                self.refresh_token,
                self.developer_token,
                self.customer_id,
            ]
        )

    async def access_token(self) -> str:
        """Return a valid access token, refreshing it when close to expiry."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        async with httpx.AsyncClient(timeout=20.0, transport=self.transport) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if response.status_code >= 400:
            raise ExternalServiceError(
                "Google OAuth token refresh failed",
                detail={"status": response.status_code, "body": response.text[:300]},
            )
        payload = response.json()
        self._access_token = str(payload["access_token"])
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._access_token


class GoogleAdsClient(AdsPlatformClient):
    """Google Ads REST adapter."""

    platform = Platform.GOOGLE

    def __init__(self, credentials: GoogleAdsCredentials) -> None:
        self._credentials = credentials
        self.is_configured = credentials.is_complete
        self._http: PlatformHTTPClient | None = None

    async def _client(self) -> PlatformHTTPClient:
        if not self.is_configured:
            raise ExternalServiceError(
                "Google Ads is not configured; set GOOGLE_ADS_* environment variables"
            )
        if self._http is None:
            token = await self._credentials.access_token()
            self._http = PlatformHTTPClient(
                GOOGLE_ADS_API_BASE,
                headers={
                    "Authorization": "Bearer " + token,
                    "developer-token": self._credentials.developer_token,
                    "Content-Type": "application/json",
                },
                transport=self._credentials.transport,
            )
        return self._http

    def _customer_path(self) -> str:
        return "customers/" + self._credentials.customer_id

    async def _mutate(self, operations: list[dict[str, Any]], service: str) -> dict[str, Any]:
        client = await self._client()
        path = GOOGLE_ADS_VERSION + "/" + self._customer_path() + "/" + service + ":mutate"
        return await client.request("POST", path, json_body={"operations": operations})

    async def update_campaign_budget(
        self, external_id: str, *, daily_budget: float
    ) -> ExecutionResult:
        amount_micros = round(daily_budget * 1_000_000)
        payload = await self._mutate(
            [
                {
                    "update": {
                        "resourceName": "campaignBudgets/" + external_id,
                        "amountMicros": str(amount_micros),
                    },
                    "updateMask": "amountMicros",
                }
            ],
            "campaignBudgets",
        )
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation="update_budget",
            external_reference=str(
                payload.get("results", [{}])[0].get("resourceName", external_id)
            ),
            detail={"amount_micros": amount_micros},
        )

    async def _set_campaign_status(
        self, external_id: str, status: str, operation: str, reason: str
    ) -> ExecutionResult:
        payload = await self._mutate(
            [
                {
                    "update": {"resourceName": "campaigns/" + external_id, "status": status},
                    "updateMask": "status",
                }
            ],
            "campaigns",
        )
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation=operation,
            external_reference=str(
                payload.get("results", [{}])[0].get("resourceName", external_id)
            ),
            detail={"status": status, "reason": reason},
        )

    async def pause_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_campaign_status(external_id, "PAUSED", "pause_campaign", reason)

    async def resume_campaign(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_campaign_status(external_id, "ENABLED", "resume_campaign", reason)

    async def _set_ad_status(
        self, external_id: str, status: str, operation: str, reason: str
    ) -> ExecutionResult:
        payload = await self._mutate(
            [
                {
                    "update": {"resourceName": "adGroupAds/" + external_id, "status": status},
                    "updateMask": "status",
                }
            ],
            "adGroupAds",
        )
        return ExecutionResult(
            success=True,
            platform=self.platform,
            operation=operation,
            external_reference=str(
                payload.get("results", [{}])[0].get("resourceName", external_id)
            ),
            detail={"status": status, "reason": reason},
        )

    async def pause_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_ad_status(external_id, "PAUSED", "pause_creative", reason)

    async def resume_creative(self, external_id: str, *, reason: str) -> ExecutionResult:
        return await self._set_ad_status(external_id, "ENABLED", "resume_creative", reason)

    async def create_creative(
        self, campaign_external_id: str, draft: CreativeDraft
    ) -> ExecutionResult:
        raise ExternalServiceError(
            "Creating Google Ads creatives requires an ad group and asset set; "
            "use the campaign setup flow instead of the optimizer action path",
            detail={"campaign_external_id": campaign_external_id, "headline": draft.headline},
        )

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        client = await self._client()
        query = (
            "SELECT campaign.id, metrics.impressions, metrics.clicks, "
            "metrics.conversions, metrics.cost_micros, segments.date "
            "FROM campaign WHERE campaign.id = " + str(external_id) + " "
            "AND segments.date BETWEEN '" + start_date + "' AND '" + end_date + "'"
        )
        payload = await client.request(
            "POST",
            GOOGLE_ADS_VERSION + "/" + self._customer_path() + "/googleAds:search",
            json_body={"query": query},
        )
        rows = [self._normalise_row(row) for row in payload.get("results", [])]
        return {"campaign_id": external_id, "source": "google", "rows": rows}

    @staticmethod
    def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
        metrics = row.get("metrics", {})
        cost_micros = float(metrics.get("costMicros", 0) or 0)
        return {
            "date": row.get("segments", {}).get("date"),
            "impressions": int(float(metrics.get("impressions", 0) or 0)),
            "clicks": int(float(metrics.get("clicks", 0) or 0)),
            "conversions": int(float(metrics.get("conversions", 0) or 0)),
            "cost": round(cost_micros / 1_000_000, 4),
        }

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


def build_google_client() -> GoogleAdsClient:
    """Construct the adapter from environment configuration."""
    import os

    credentials = GoogleAdsCredentials(
        client_id=os.getenv("GOOGLE_ADS_CLIENT_ID", ""),
        client_secret=os.getenv("GOOGLE_ADS_CLIENT_SECRET", ""),
        refresh_token=os.getenv("GOOGLE_ADS_REFRESH_TOKEN", ""),
        developer_token=os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", ""),
        customer_id=os.getenv("GOOGLE_ADS_CUSTOMER_ID", ""),
    )
    if not credentials.is_complete:
        logger.info("google_ads_unconfigured")
    return GoogleAdsClient(credentials)
