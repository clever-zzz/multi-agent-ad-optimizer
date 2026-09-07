"""The single entry point every agent uses to call a model.

Responsibilities that must not live inside individual agents:
- bounded concurrency so a burst of campaigns cannot exhaust the provider
- retries with jittered backoff on transient failures
- response caching keyed on the exact prompt
- a monthly spend budget that hard-stops generation before the bill runs away
- graceful degradation to the deterministic mock model
- metrics and token accounting for every call
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..core.config import LLMProvider, LLMSettings, get_settings
from ..core.errors import BudgetExceededError, ExternalServiceError
from ..core.logging import get_logger
from ..core.metrics import (
    LLM_CALLS_TOTAL,
    LLM_LATENCY_SECONDS,
    LLM_SPEND_USD_TOTAL,
    LLM_TOKENS_TOTAL,
)
from .base import CompletionRequest, CompletionResult, LanguageModel, Usage
from .mock import MockLanguageModel
from .structured import parse_model, parse_model_list

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class SpendLedger(Protocol):
    """Persistence hook for token and cost accounting."""

    async def record(self, *, run_id: str | None, agent: str, result: CompletionResult) -> None: ...

    async def month_to_date_usd(self) -> float: ...


class NullSpendLedger:
    """No-op ledger used when the database is unavailable."""

    def __init__(self) -> None:
        self.total_usd = 0.0
        self.total_tokens = 0

    async def record(self, *, run_id: str | None, agent: str, result: CompletionResult) -> None:
        self.total_usd += result.usage.cost_usd
        self.total_tokens += result.usage.total_tokens

    async def month_to_date_usd(self) -> float:
        return self.total_usd


class LLMGateway:
    """Resilient model access with caching, budgets and fallback."""

    def __init__(
        self,
        primary: LanguageModel,
        *,
        settings: LLMSettings | None = None,
        cache: Any = None,
        ledger: SpendLedger | None = None,
    ) -> None:
        self._settings = settings or get_settings().llm
        self._primary = primary
        self._fallback = MockLanguageModel()
        self._cache = cache
        self._ledger = ledger or NullSpendLedger()
        self._semaphore = asyncio.Semaphore(self._settings.concurrency)
        self._budget_exceeded = False

    @property
    def provider(self) -> LLMProvider:
        return self._primary.provider

    @property
    def model(self) -> str:
        return self._primary.model

    @property
    def is_live(self) -> bool:
        """True when a real provider is configured rather than the mock."""
        return self._primary.provider != LLMProvider.MOCK and self._primary.is_available

    async def complete(
        self, request: CompletionRequest, *, run_id: str | None = None, agent: str = ""
    ) -> CompletionResult:
        """Execute a completion with caching, retries and degradation."""
        await self._enforce_budget()

        cached = await self._read_cache(request)
        if cached is not None:
            return cached

        async with self._semaphore:
            result = await self._call_with_retry(request)

        await self._write_cache(request, result)
        self._observe(result)
        await self._ledger.record(run_id=run_id, agent=agent, result=result)
        return result

    async def generate(
        self,
        model_class: type[T],
        request: CompletionRequest,
        *,
        many: bool = False,
        max_items: int = 20,
        run_id: str | None = None,
        agent: str = "",
    ) -> T | list[T]:
        """Complete and validate the response against a pydantic schema."""
        result = await self.complete(request, run_id=run_id, agent=agent)
        if many:
            return parse_model_list(model_class, result.text, max_items=max_items)
        return parse_model(model_class, result.text)

    async def _enforce_budget(self) -> None:
        """Hard-stop when the monthly spend cap has been reached."""
        limit = self._settings.monthly_budget_usd
        if limit <= 0 or self._budget_exceeded:
            if self._budget_exceeded:
                raise BudgetExceededError(
                    "Monthly LLM budget of $" + format(limit, ".2f") + " has been reached"
                )
            return

        spent = await self._ledger.month_to_date_usd()
        if spent >= limit:
            self._budget_exceeded = True
            logger.error("llm_budget_exceeded", spent_usd=round(spent, 4), limit_usd=limit)
            raise BudgetExceededError(
                "Monthly LLM budget of $" + format(limit, ".2f") + " has been reached"
            )
        if spent >= limit * 0.8:
            logger.warning(
                "llm_budget_threshold", spent_usd=round(spent, 4), limit_usd=limit, pct=80
            )

    async def _read_cache(self, request: CompletionRequest) -> CompletionResult | None:
        if not (self._settings.cache_enabled and self._cache is not None):
            return None
        import orjson

        payload = await self._cache.get_json("llm:" + request.cache_key)
        if payload is None:
            return None
        try:
            usage_raw = payload.get("usage", {})
            return CompletionResult(
                text=payload["text"],
                usage=Usage(
                    prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                    completion_tokens=int(usage_raw.get("completion_tokens", 0)),
                    cost_usd=0.0,
                ),
                provider=LLMProvider(payload.get("provider", self._primary.provider.value)),
                model=str(payload.get("model", self._primary.model)),
                latency_ms=0,
                cached=True,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("llm_cache_decode_failed", error=str(exc))
            _ = orjson
            return None

    async def _write_cache(self, request: CompletionRequest, result: CompletionResult) -> None:
        if not (self._settings.cache_enabled and self._cache is not None):
            return
        if result.degraded or result.error:
            return
        await self._cache.set_json(
            "llm:" + request.cache_key,
            {
                "text": result.text,
                "provider": result.provider.value,
                "model": result.model,
                "usage": result.usage.to_dict(),
            },
            self._settings.cache_ttl_seconds,
        )

    async def _call_with_retry(self, request: CompletionRequest) -> CompletionResult:
        attempts = max(1, self._settings.max_retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                result = await self._primary.complete(request)
                if not result.text.strip():
                    raise ExternalServiceError("Provider returned an empty completion")
                LLM_CALLS_TOTAL.labels(
                    provider=result.provider.value, model=result.model, outcome="success"
                ).inc()
                return result
            except ExternalServiceError as exc:
                last_error = exc
                LLM_CALLS_TOTAL.labels(
                    provider=self._primary.provider.value,
                    model=self._primary.model,
                    outcome="error",
                ).inc()
                if attempt >= attempts:
                    break
                delay = self._settings.retry_base_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "llm_attempt_failed", attempt=attempt, delay_s=round(delay, 2), error=str(exc)
                )
                await asyncio.sleep(min(delay, 8.0))

        if self._settings.fail_open_to_mock:
            logger.warning("llm_degrading_to_mock", error=str(last_error))
            LLM_CALLS_TOTAL.labels(
                provider=self._primary.provider.value, model=self._primary.model, outcome="degraded"
            ).inc()
            degraded = await self._fallback.complete(request)
            return CompletionResult(
                text=degraded.text,
                usage=degraded.usage,
                provider=degraded.provider,
                model=degraded.model,
                latency_ms=degraded.latency_ms,
                degraded=True,
                error=str(last_error) if last_error else None,
            )

        raise ExternalServiceError(
            "Model provider failed after " + str(attempts) + " attempts",
            detail={"error": str(last_error) if last_error else "unknown"},
        )

    def _observe(self, result: CompletionResult) -> None:
        started = time.perf_counter()
        LLM_LATENCY_SECONDS.labels(provider=result.provider.value).observe(
            (result.latency_ms or int((time.perf_counter() - started) * 1000)) / 1000.0
        )
        LLM_TOKENS_TOTAL.labels(
            provider=result.provider.value, model=result.model, kind="prompt"
        ).inc(result.usage.prompt_tokens)
        LLM_TOKENS_TOTAL.labels(
            provider=result.provider.value, model=result.model, kind="completion"
        ).inc(result.usage.completion_tokens)
        if result.usage.cost_usd > 0:
            LLM_SPEND_USD_TOTAL.labels(provider=result.provider.value, model=result.model).inc(
                result.usage.cost_usd
            )

    async def close(self) -> None:
        await self._primary.close()


def build_gateway(
    settings: LLMSettings | None = None, *, cache: Any = None, ledger: SpendLedger | None = None
) -> LLMGateway:
    """Construct a gateway for the configured provider."""
    cfg = settings or get_settings().llm

    primary: LanguageModel
    if cfg.provider == LLMProvider.MOCK:
        primary = MockLanguageModel()
    else:
        from .openai_provider import OpenAICompatibleModel

        primary = OpenAICompatibleModel(cfg)
        if not primary.is_available:
            logger.warning("llm_provider_unavailable_using_mock", provider=cfg.provider.value)
            primary = MockLanguageModel()

    logger.info("llm_gateway_built", provider=primary.provider.value, model=primary.model)
    return LLMGateway(primary, settings=cfg, cache=cache, ledger=ledger)
