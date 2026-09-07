"""Creative Agent - generate ad copy variants for underperforming campaigns.

Generation is gated on evidence: a campaign only receives new copy when a
metric is actually weak, which keeps model spend proportional to need. Output is
validated against CreativeVariant, so a malformed model response degrades to the
deterministic rule generator rather than poisoning the run.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..core.logging import get_logger
from ..domain.enums import AgentName
from ..domain.kpi import CTR_PRIOR_MEAN, CVR_PRIOR_MEAN, PerformanceSnapshot
from ..llm.base import CompletionRequest
from ..llm.structured import StructuredOutputError
from ..orchestrator.state import AgentState
from ..schemas.agent import CreativeVariant
from .base import AgentContext, BaseAgent

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are a direct-response copywriter for paid social and search.
Write ad variants that are specific, benefit-led and free of unverifiable claims.

Rules:
- Never invent prices, discounts, awards, guarantees or statistics.
- Keep the headline at most 60 characters and the description at most 200.
- Each variant must pursue a different emotional angle.
- Respond with JSON only: an array of objects with keys
  headline, description, cta_text, target_emotion, rationale.
  target_emotion must be one of: urgency, trust, curiosity, benefit, social_proof."""

EMOTION_STRATEGY: dict[str, tuple[str, ...]] = {
    "weak_ctr": ("curiosity", "social_proof", "urgency"),
    "weak_cvr": ("benefit", "trust", "social_proof"),
    "weak_both": ("benefit", "urgency", "trust"),
    "healthy": ("curiosity", "benefit", "social_proof"),
}

HEADLINE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "urgency": (
        "Last chance: {product} offer ends soon",
        "Your {product} window is closing",
        "Only a few days left on {product}",
    ),
    "trust": (
        "{product}, built to be relied on",
        "The {product} choice that holds up",
        "Verified quality in every {product}",
    ),
    "curiosity": (
        "What changes when you switch to {product}",
        "The {product} detail most people miss",
        "Why {product} keeps getting recommended",
    ),
    "benefit": (
        "Get more from {product} in less time",
        "{product}: the outcome without the hassle",
        "Everything you want from {product}, included",
    ),
    "social_proof": (
        "The {product} buyers keep coming back to",
        "{product} is the repeat purchase favourite",
        "Join the customers who chose {product}",
    ),
}

DESCRIPTION_TEMPLATES: dict[str, str] = {
    "urgency": (
        "Current terms on {product} are time-boxed. Review the offer and "
        "decide before it rotates out."
    ),
    "trust": (
        "{product} is built on documented specifications and a straightforward support "
        "path, so the decision is easy to defend."
    ),
    "curiosity": (
        "Most {product} comparisons stop at the headline spec. Look at how it behaves in "
        "week three and the difference becomes obvious."
    ),
    "benefit": (
        "{product} removes the steps between deciding and getting the result. Set up "
        "once, then it works in the background."
    ),
    "social_proof": (
        "Repeat customers are the clearest signal available. {product} keeps earning "
        "them, which is why it stays in rotation."
    ),
}

CTA_BY_EMOTION: dict[str, str] = {
    "urgency": "See the offer",
    "trust": "Learn more",
    "curiosity": "See how it works",
    "benefit": "Get started",
    "social_proof": "Read reviews",
}

MAX_VARIANTS_PER_CAMPAIGN = 4


class CreativeAgent(BaseAgent):
    """Produces validated copy variants and explains why each was generated."""

    name = AgentName.CREATIVE

    async def run(self, state: AgentState, context: AgentContext) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0) or 0)
        snapshots = self._snapshots(state)
        targets = [s for s in snapshots if self._needs_new_creative(s, context)]

        if not targets:
            message = self._message(
                "No campaign met the evidence gate for new creative this iteration.",
                iteration=iteration,
            )
            return {
                "current_agent": self.name.value,
                "agent_messages": [message],
                "_summary": {"campaigns_targeted": 0, "variants": 0},
            }

        variants: list[dict[str, Any]] = []
        llm_count = 0
        for snapshot in targets[: MAX_VARIANTS_PER_CAMPAIGN * 3]:
            generated, used_llm = await self._generate(snapshot, context)
            llm_count += 1 if used_llm else 0
            for index, variant in enumerate(generated):
                payload = variant.model_dump()
                payload["campaign_id"] = snapshot.campaign_id
                payload["campaign_name"] = snapshot.campaign_name
                payload["iteration"] = iteration
                payload["ab_group"] = variant.ab_group or "variant_" + chr(ord("a") + index)
                variants.append(payload)

        message = self._message(
            "Generated "
            + str(len(variants))
            + " creative variants across "
            + str(len(targets))
            + " campaign(s); "
            + str(llm_count)
            + " produced by the model gateway.",
            iteration=iteration,
        )
        logger.info(
            "creative_completed",
            run_id=context.run_id,
            campaigns=len(targets),
            variants=len(variants),
            llm_campaigns=llm_count,
        )

        return {
            "new_creatives": variants,
            "current_agent": self.name.value,
            "agent_messages": [message],
            "_summary": {
                "campaigns_targeted": len(targets),
                "variants": len(variants),
                "llm_campaigns": llm_count,
            },
        }

    def _snapshots(self, state: AgentState) -> list[PerformanceSnapshot]:
        restored: list[PerformanceSnapshot] = []
        for metric in state.get("metrics") or []:
            if isinstance(metric, PerformanceSnapshot):
                restored.append(metric)
            elif isinstance(metric, dict):
                try:
                    restored.append(PerformanceSnapshot.model_validate(metric))
                except Exception as exc:
                    logger.warning("creative_skipped_invalid_metric", error=str(exc))
        return restored

    def _needs_new_creative(self, snapshot: PerformanceSnapshot, context: AgentContext) -> bool:
        """Gate on volume plus a genuinely weak signal."""
        if snapshot.impressions < context.optimization.min_impressions_for_alerts:
            return False
        return (
            snapshot.ctr < context.optimization.alert_ctr_floor * 2
            or snapshot.cvr < CVR_PRIOR_MEAN * 0.8
            or snapshot.ctr < CTR_PRIOR_MEAN * 0.7
        )

    def _diagnose(self, snapshot: PerformanceSnapshot, context: AgentContext) -> str:
        weak_ctr = snapshot.ctr < CTR_PRIOR_MEAN
        weak_cvr = snapshot.cvr < CVR_PRIOR_MEAN
        if weak_ctr and weak_cvr:
            return "weak_both"
        if weak_ctr:
            return "weak_ctr"
        if weak_cvr:
            return "weak_cvr"
        _ = context
        return "healthy"

    async def _generate(
        self, snapshot: PerformanceSnapshot, context: AgentContext
    ) -> tuple[list[CreativeVariant], bool]:
        """Try the model first, then fall back to the deterministic generator."""
        diagnosis = self._diagnose(snapshot, context)
        emotions = EMOTION_STRATEGY[diagnosis]
        existing = {
            str(c.get("headline", "")).strip().lower()
            for c in context.existing_creatives.get(snapshot.campaign_id, [])
        }

        request = CompletionRequest(
            task="creative_variants",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=self._build_prompt(snapshot, diagnosis, emotions),
            schema_hint="[{"
            + '"headline": str, "description": str, "cta_text": str, '
            + '"target_emotion": str, "rationale": str'
            + "}]",
            metadata={"campaign_id": snapshot.campaign_id},
        )

        try:
            result = await context.gateway.complete(
                request, run_id=context.run_id, agent=self.name.value
            )
            from ..llm.structured import parse_model_list

            parsed = parse_model_list(
                CreativeVariant, result.text, max_items=MAX_VARIANTS_PER_CAMPAIGN
            )
            unique = self._dedupe(parsed, existing)
            if unique:
                return [v.model_copy(update={"source": "llm"}) for v in unique], True
            logger.info("creative_llm_returned_duplicates", campaign=snapshot.campaign_id)
        except (StructuredOutputError, ValueError) as exc:
            logger.warning(
                "creative_llm_fallback", campaign=snapshot.campaign_id, error=str(exc)[:200]
            )
        except Exception as exc:
            logger.warning(
                "creative_llm_error", campaign=snapshot.campaign_id, error=str(exc)[:200]
            )

        return self._rule_based(snapshot, emotions, existing), False

    def _build_prompt(
        self, snapshot: PerformanceSnapshot, diagnosis: str, emotions: tuple[str, ...]
    ) -> str:
        return (
            "Campaign: " + (snapshot.campaign_name or snapshot.campaign_id) + "\n"
            "Diagnosis: " + diagnosis + "\n"
            "Observed CTR: "
            + format(snapshot.ctr, ".3%")
            + " | Observed CVR: "
            + format(snapshot.cvr, ".3%")
            + "\n"
            "Predicted CTR: "
            + format(snapshot.predicted_ctr, ".3%")
            + " | Predicted CVR: "
            + format(snapshot.predicted_cvr, ".3%")
            + "\n"
            "Impressions: "
            + str(snapshot.impressions)
            + " | Clicks: "
            + str(snapshot.clicks)
            + " | Conversions: "
            + str(snapshot.conversions)
            + "\n\n"
            "Write "
            + str(MAX_VARIANTS_PER_CAMPAIGN)
            + " variants using these angles in order: "
            + ", ".join(emotions)
            + "."
        )

    def _rule_based(
        self,
        snapshot: PerformanceSnapshot,
        emotions: tuple[str, ...],
        existing: set[str],
    ) -> list[CreativeVariant]:
        """Deterministic template generation, stable across runs for one input."""
        product = _product_of(snapshot.campaign_name or snapshot.campaign_id)
        seed = int(hashlib.sha256(snapshot.campaign_id.encode()).hexdigest()[:8], 16)
        variants: list[CreativeVariant] = []

        for index, emotion in enumerate(emotions[:MAX_VARIANTS_PER_CAMPAIGN]):
            templates = HEADLINE_TEMPLATES[emotion]
            headline = templates[(seed + index) % len(templates)].format(product=product)
            if headline.strip().lower() in existing:
                headline = templates[(seed + index + 1) % len(templates)].format(product=product)

            variants.append(
                CreativeVariant(
                    headline=headline[:120],
                    description=DESCRIPTION_TEMPLATES[emotion].format(product=product)[:600],
                    cta_text=CTA_BY_EMOTION[emotion],
                    target_emotion=emotion,
                    ab_group="variant_" + chr(ord("a") + index),
                    rationale=(
                        "Rule-based " + emotion + " angle for a campaign diagnosed as "
                        "underperforming on "
                        + (
                            "click-through"
                            if emotion in ("curiosity", "urgency", "social_proof")
                            else "conversion"
                        )
                    ),
                    source="rule",
                )
            )
        return variants

    @staticmethod
    def _dedupe(variants: list[CreativeVariant], existing: set[str]) -> list[CreativeVariant]:
        seen: set[str] = set(existing)
        unique: list[CreativeVariant] = []
        for variant in variants:
            key = variant.headline.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(variant)
        return unique


def _product_of(campaign_name: str) -> str:
    """Extract a readable product name from a campaign title."""
    for separator in (" - ", " | ", " / ", " -- "):
        if separator in campaign_name:
            return campaign_name.split(separator)[0].strip()[:40] or "this offer"
    return campaign_name.strip()[:40] or "this offer"
