"""Shared HTTP plumbing for the real platform adapters."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ...core.metrics import LLM_LATENCY_SECONDS  # reused histogram bucket shape

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class RetryableHTTPError(Exception):
    """Raised for responses worth retrying."""


class PlatformHTTPClient:
    """Thin httpx wrapper with bounded retries and jittered backoff."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._headers = headers or {}
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                headers={"Accept": "application/json", **self._headers},
                follow_redirects=False,
            )
        return self._client

    @retry(
        retry=retry_if_exception_type(RetryableHTTPError),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=0.5, max=8.0, jitter=0.5),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform a request, retrying only on transient failures."""
        client = await self._ensure_client()
        url = path if path.startswith("http") else "/" + path.lstrip("/")
        try:
            response = await client.request(method, url, params=params, json=json_body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.warning("platform_transport_error", url=url, error=str(exc))
            raise RetryableHTTPError(str(exc)) from exc

        if response.status_code in RETRYABLE_STATUS:
            logger.warning("platform_retryable_status", url=url, status=response.status_code)
            raise RetryableHTTPError("Upstream returned " + str(response.status_code))

        if response.status_code >= 400:
            body = response.text[:500]
            logger.error("platform_error", url=url, status=response.status_code, body=body)
            msg = "Platform request failed with status " + str(response.status_code) + ": " + body
            raise ExternalServiceError(msg, detail={"url": url, "status": response.status_code})

        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalServiceError("Platform returned a non-JSON body") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


async def with_timeout(coro: Any, seconds: float) -> Any:
    """Apply an explicit wall-clock timeout to an adapter call."""
    return await asyncio.wait_for(coro, timeout=seconds)


def observe_latency(provider: str, elapsed: float) -> None:
    """Record adapter latency on the shared histogram."""
    LLM_LATENCY_SECONDS.labels(provider="ads:" + provider).observe(elapsed)
