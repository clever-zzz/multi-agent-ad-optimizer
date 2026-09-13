"""Provider-agnostic model contracts and pricing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.config import LLMProvider

# USD per one million tokens, prompt price first. Only used for budget
# guardrails and reporting, so an approximate table is acceptable here -- but it
# is a *fallback*, not the truth for a given deployment. Vendor list prices
# drift and regional billing differs, so LLMSettings.pricing (LLM__PRICING)
# overrides any entry; a model with no entry of its own falls back to "default".
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "claude-sonnet-4": (3.00, 15.00),
    "qwen-turbo": (0.05, 0.20),
    "qwen-plus": (0.40, 1.20),
    "qwen-max": (1.60, 6.40),
    "default": (1.00, 3.00),
}

PricingTable = Mapping[str, tuple[float, float]]
"""Model name to (prompt, completion) USD per one million tokens."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for a single completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int | float]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """A single model call."""

    task: str
    system_prompt: str
    user_prompt: str
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = True
    schema_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_key(self) -> str:
        """Stable key covering everything that affects the response."""
        import hashlib

        digest = hashlib.sha256()
        for part in (self.task, self.system_prompt, self.user_prompt, self.schema_hint):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(str(self.json_mode).encode())
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A model response plus the telemetry needed to operate it."""

    text: str
    usage: Usage
    provider: LLMProvider
    model: str
    latency_ms: int
    cached: bool = False
    degraded: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "cached": self.cached,
            "degraded": self.degraded,
            "error": self.error,
            "usage": self.usage.to_dict(),
        }


DeltaHandler = Callable[[str], Awaitable[None]]
"""Awaitable sink for raw text fragments as a streaming provider emits them.

Fragments are presentation sugar, not a second source of truth about the answer:
they belong to whichever attempt produced them, a cache hit delivers the whole
text as one fragment, and a degraded call delivers none at all. Consumers must
treat CompletionResult.text as the authoritative response.
"""


class LanguageModel(Protocol):
    """Minimal contract every provider must satisfy."""

    provider: LLMProvider
    model: str
    is_available: bool

    async def complete(
        self, request: CompletionRequest, *, on_delta: DeltaHandler | None = None
    ) -> CompletionResult: ...

    async def close(self) -> None: ...


def resolve_pricing(model: str, overrides: PricingTable | None = None) -> tuple[float, float]:
    """Return the (prompt, completion) USD price per million tokens for *model*.

    Deployment overrides win over the built-in table. An explicit zero price is
    honoured rather than read as "no entry" -- free tiers and promotional credit
    are real, and quietly billing them at the default would defeat the point of
    stating them.
    """
    table: PricingTable = {**DEFAULT_PRICING, **overrides} if overrides else DEFAULT_PRICING
    prices = table.get(model)
    if prices is None:
        prices = table.get("default", DEFAULT_PRICING["default"])
    return prices


def is_priced(model: str, overrides: PricingTable | None = None) -> bool:
    """True when *model* has a price of its own instead of the "default" fallback."""
    if overrides and model in overrides:
        return True
    return model in DEFAULT_PRICING and model != "default"


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    overrides: PricingTable | None = None,
) -> float:
    """Estimate USD cost from the pricing table plus any deployment overrides."""
    prompt_price, completion_price = resolve_pricing(model, overrides)
    return (prompt_tokens * prompt_price + completion_tokens * completion_price) / 1_000_000.0
