"""Deterministic offline model.

Produces schema-valid output derived from the prompt hash so repeated runs are
reproducible. This is what makes the whole platform runnable with no API key
and no network, and it is also the automatic fallback when a real provider
fails.
"""

from __future__ import annotations

import hashlib
import json
import random
import time

from ..core.config import LLMProvider
from ..core.logging import get_logger
from .base import CompletionRequest, CompletionResult, Usage

logger = get_logger(__name__)

EMOTION_BANK = {
    "urgency": ["Limited time", "Final hours", "Ends tonight", "Last chance"],
    "trust": ["Trusted by thousands", "Certified quality", "Backed by warranty"],
    "curiosity": ["What nobody tells you", "The surprising truth", "You may not know this"],
    "benefit": ["Save more today", "Effortless results", "Everything included"],
    "social_proof": ["Best seller", "Customer favourite", "Trending now"],
}

CTA_BANK = ["Shop now", "Learn more", "Get started", "Claim offer", "Try free"]


class MockLanguageModel:
    """Rule-based generator that satisfies the LanguageModel protocol."""

    provider = LLMProvider.MOCK
    model = "mock-deterministic-v1"
    is_available = True

    def __init__(self, *, seed_salt: str = "") -> None:
        self._salt = seed_salt

    def _rng(self, request: CompletionRequest) -> random.Random:
        digest = hashlib.sha256(
            (self._salt + request.task + request.user_prompt).encode("utf-8")
        ).hexdigest()
        # Seeded from a digest so the same prompt always yields the same mock
        # completion. Determinism is the point; this never guards anything.
        return random.Random(int(digest[:16], 16))  # noqa: S311

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        payload = self._render(request)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        prompt_tokens = max(1, (len(request.system_prompt) + len(request.user_prompt)) // 4)
        completion_tokens = max(1, len(text) // 4)

        logger.debug(
            "mock_llm_completion",
            task=request.task,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return CompletionResult(
            text=text,
            usage=Usage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=0.0
            ),
            provider=self.provider,
            model=self.model,
            latency_ms=elapsed_ms,
        )

    def _render(self, request: CompletionRequest) -> object:
        rng = self._rng(request)
        task = request.task

        if task == "creative_variants":
            return self._creative_variants(request, rng)
        if task == "audience_insight":
            return self._audience_insight(request, rng)
        if task == "optimization_rationale":
            return self._rationale(request, rng)
        if task == "alert_summary":
            return self._alert_summary(request, rng)
        return {"task": task, "note": "No mock generator for this task", "items": []}

    def _creative_variants(
        self, request: CompletionRequest, rng: random.Random
    ) -> list[dict[str, str]]:
        emotions = rng.sample(list(EMOTION_BANK), k=min(4, len(EMOTION_BANK)))
        variants = []
        for index, emotion in enumerate(emotions):
            hook = rng.choice(EMOTION_BANK[emotion])
            variants.append(
                {
                    "headline": hook + " - " + _subject_of(request.user_prompt),
                    "description": (
                        hook.lower()
                        + " on "
                        + _subject_of(request.user_prompt)
                        + ". Built for people who want results without the guesswork."
                    ),
                    "cta_text": rng.choice(CTA_BANK),
                    "target_emotion": emotion,
                    "ab_group": "variant_" + chr(ord("a") + index),
                }
            )
        return variants

    def _audience_insight(
        self, request: CompletionRequest, rng: random.Random
    ) -> dict[str, object]:
        return {
            "hypothesis": "Conversion is concentrated in higher-intent segments",
            "recommended_exclusions": [],
            "confidence": round(rng.uniform(0.55, 0.85), 2),
            "notes": "Derived from observed conversion share versus delivery share.",
        }

    def _rationale(self, request: CompletionRequest, rng: random.Random) -> dict[str, object]:
        return {
            "summary": "Reallocate spend toward campaigns with a statistically stronger return",
            "risks": ["Short-term volume drop on reduced campaigns"],
            "confidence": round(rng.uniform(0.6, 0.9), 2),
        }

    def _alert_summary(self, request: CompletionRequest, rng: random.Random) -> dict[str, object]:
        return {
            "summary": "Detected efficiency degradation on one or more campaigns",
            "priority": rng.choice(["high", "medium", "low"]),
        }

    async def close(self) -> None:
        return None


def _subject_of(prompt: str) -> str:
    """Pull a product-ish subject out of the prompt for readable mock copy."""
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("campaign:", "product:", "name:")):
            return stripped.split(":", 1)[1].strip()[:60]
    return "your product"
