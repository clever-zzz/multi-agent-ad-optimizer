"""OpenAI-compatible chat completion provider.

Speaks the standard /chat/completions contract over httpx, so it works with
OpenAI, Azure OpenAI, DeepSeek, Moonshot, vLLM and Ollama by changing the base
URL only. No vendor SDK, which keeps the dependency surface small.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..core.config import LLMProvider, LLMSettings
from ..core.errors import ExternalServiceError
from ..core.logging import get_logger
from .base import CompletionRequest, CompletionResult, LanguageModel, Usage, estimate_cost

logger = get_logger(__name__)

DEFAULT_BASE_URLS: dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "https://api.openai.com/v1",
    LLMProvider.AZURE_OPENAI: "",
    LLMProvider.OPENAI_COMPATIBLE: "",
}


class OpenAICompatibleModel(LanguageModel):
    """Chat completion client for any OpenAI-compatible endpoint."""

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings
        self.provider = settings.provider
        self.model = settings.model
        self.is_available = bool(settings.api_key.get_secret_value())
        self._client: httpx.AsyncClient | None = None

    def _base_url(self) -> str:
        if self._settings.api_base:
            return self._settings.api_base.rstrip("/")
        return DEFAULT_BASE_URLS.get(self.provider, "").rstrip("/")

    def _endpoint(self) -> str:
        base = self._base_url()
        if not base:
            raise ExternalServiceError(
                "LLM__API_BASE is required for provider " + self.provider.value
            )
        if self.provider == LLMProvider.AZURE_OPENAI:
            version = self._settings.api_version or "2024-10-21"
            deployment = self.model.replace(".", "-")
            return (
                base
                + "/openai/deployments/"
                + deployment
                + "/chat/completions?api-version="
                + version
            )
        return base + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self._settings.api_key.get_secret_value()
        if self.provider == LLMProvider.AZURE_OPENAI:
            return {"api-key": key, "Content-Type": "application/json"}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        if self._settings.organization:
            headers["OpenAI-Organization"] = self._settings.organization
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.timeout_seconds),
                headers=self._headers(),
            )
        return self._client

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if not self.is_available:
            raise ExternalServiceError("LLM provider has no API key configured")

        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature
            if request.temperature is not None
            else self._settings.temperature,
            "max_tokens": request.max_tokens or self._settings.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        client = await self._ensure_client()
        try:
            response = await client.post(self._endpoint(), json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ExternalServiceError("LLM request failed: " + str(exc)) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            body = response.text[:400]
            logger.error("llm_http_error", status=response.status_code, body=body)
            raise ExternalServiceError(
                "LLM provider returned " + str(response.status_code),
                detail={"status": response.status_code, "body": body},
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ExternalServiceError("LLM provider returned no choices")

        text = str((choices[0].get("message") or {}).get("content") or "")
        raw_usage = data.get("usage") or {}
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)

        return CompletionResult(
            text=text,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=estimate_cost(self.model, prompt_tokens, completion_tokens),
            ),
            provider=self.provider,
            model=str(data.get("model", self.model)),
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
