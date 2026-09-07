"""Provider-agnostic model contracts and pricing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.config import LLMProvider

# USD per one million tokens. Only used for budget guardrails and reporting, so
# an approximate table is acceptable; override via LLM__PRICING in production.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "deepseek-chat": (0.27, 1.10),
    "claude-sonnet-4": (3.00, 15.00),
    "default": (1.00, 3.00),
}


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


class LanguageModel(Protocol):
    """Minimal contract every provider must satisfy."""

    provider: LLMProvider
    model: str
    is_available: bool

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def close(self) -> None: ...


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost from the pricing table."""
    prompt_price, completion_price = DEFAULT_PRICING.get(model, DEFAULT_PRICING["default"])
    return (prompt_tokens * prompt_price + completion_tokens * completion_price) / 1_000_000.0
