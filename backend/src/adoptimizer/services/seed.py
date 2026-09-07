"""Deterministic demo dataset.

Seeded with a fixed RNG so every environment gets the same data, and shaped so
the optimizer has real work to do: some campaigns are efficient, some are
bleeding budget, and one creative is provably bad. Without that spread a demo
run produces no alerts and no actions, which hides whether the system works.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utc_today
from ..core.config import SecuritySettings
from ..core.ids import new_id
from ..core.logging import get_logger
from ..core.security import PasswordHasherService, Role
from ..domain.enums import CampaignStatus, CreativeStatus, Platform
from ..infra.db.models import Campaign, Creative, DailyMetric
from ..repositories.users import UserRepository

logger = get_logger(__name__)

SEED = 20260906

# name, platform, daily_budget, ctr, cvr, cpc, aov, quality
CAMPAIGN_BLUEPRINTS: list[tuple[str, Platform, float, float, float, float, float, str]] = [
    ("Aurora Smart Watch - Search", Platform.GOOGLE, 1800.0, 0.048, 0.085, 1.90, 249.0, "strong"),
    (
        "Nimbus ANC Headphones - Prospecting",
        Platform.META,
        2400.0,
        0.031,
        0.062,
        1.20,
        179.0,
        "healthy",
    ),
    ("Lumen Air Purifier - Retargeting", Platform.META, 900.0, 0.055, 0.110, 0.85, 329.0, "strong"),
    ("Terra Protein Pack - Broad", Platform.TIKTOK, 1500.0, 0.004, 0.021, 0.45, 59.0, "weak"),
    (
        "Volt Cordless Drill - Demand Gen",
        Platform.GOOGLE,
        1200.0,
        0.012,
        0.038,
        2.60,
        149.0,
        "struggling",
    ),
    ("Halo Smart Lock - Launch", Platform.TIKTOK, 700.0, 0.026, 0.055, 0.65, 199.0, "healthy"),
    ("Echo Desk Lamp - Always On", Platform.META, 450.0, 0.008, 0.030, 0.35, 45.0, "weak"),
    ("Pulse Fitness Band - Seasonal", Platform.GOOGLE, 2000.0, 0.038, 0.070, 1.45, 129.0, "strong"),
]

CREATIVE_COPY: list[tuple[str, str, str, str]] = [
    (
        "Engineered for the hours you actually use",
        "Battery, sensors and sync that stay out of the way.",
        "Shop now",
        "benefit",
    ),
    (
        "The upgrade people keep talking about",
        "Verified specifications, honest support, no fine print.",
        "Learn more",
        "trust",
    ),
    (
        "See what changes in week three",
        "Most comparisons stop at the spec sheet. This one does not.",
        "See how it works",
        "curiosity",
    ),
    (
        "Back in stock for a short window",
        "Current pricing is time-boxed and rotates without notice.",
        "See the offer",
        "urgency",
    ),
    (
        "Chosen again and again by repeat buyers",
        "Retention is the clearest signal we have.",
        "Read reviews",
        "social_proof",
    ),
]


async def seed_database(
    session: AsyncSession,
    *,
    admin_email: str,
    admin_password: str,
    security: SecuritySettings | None = None,
) -> dict[str, Any]:
    """Populate a fresh database with an operator and a realistic dataset."""
    created = await _ensure_admin(
        session, admin_email=admin_email, admin_password=admin_password, security=security
    )
    existing = await session.scalar(select(func.count()).select_from(Campaign))
    if existing:
        logger.info("seed_skipped_existing_data", campaigns=int(existing))
        return {
            "created_admin": created,
            "campaigns": 0,
            "creatives": 0,
            "daily_rows": 0,
            "skipped": True,
        }

    # A fixed seed is what makes the demo dataset reproducible run to run.
    rng = random.Random(SEED)  # noqa: S311
    today = utc_today()
    campaigns: list[Campaign] = []
    creatives: list[Creative] = []
    daily_rows: list[DailyMetric] = []

    for index, (name, platform, daily_budget, ctr, cvr, cpc, aov, quality) in enumerate(
        CAMPAIGN_BLUEPRINTS
    ):
        campaign = Campaign(
            id=new_id("camp"),
            name=name,
            platform=platform.value,
            status=CampaignStatus.ACTIVE.value
            if quality != "retired"
            else CampaignStatus.PAUSED.value,
            external_id="ext_" + platform.value + "_" + str(1000 + index),
            daily_budget=daily_budget,
            total_budget=round(daily_budget * 30, 2),
            target_cpa=round(aov * 0.35, 2),
            target_roas=2.5 if quality == "strong" else 2.0,
            start_date=today - timedelta(days=45),
            objective="conversions",
            target_audience=_audience_for(platform),
            current_bid_cpm=round(ctr * cvr * aov * 0.35 * 1000, 4),
        )
        campaigns.append(campaign)

        for slot, (headline, description, cta, emotion) in enumerate(CREATIVE_COPY[:4]):
            creative_quality = "bad" if (quality == "weak" and slot == 3) else quality
            creatives.append(
                Creative(
                    id=new_id("cre"),
                    campaign_id=campaign.id,
                    headline=headline,
                    description=description,
                    cta_text=cta,
                    target_emotion=emotion,
                    creative_type="text",
                    status=CreativeStatus.ACTIVE.value,
                    ab_group="control" if slot == 0 else "variant_" + chr(ord("a") + slot - 1),
                    origin="human",
                    score=None,
                )
            )
            _ = creative_quality

        for day_offset in range(21):
            stat_date = today - timedelta(days=day_offset)
            weekday_factor = 1.15 if stat_date.weekday() >= 5 else 1.0
            trend = 1.0 + (20 - day_offset) * (0.012 if quality == "strong" else -0.006)
            impressions = int(
                daily_budget
                / max(cpc * ctr, 0.0001)
                * weekday_factor
                * trend
                * rng.uniform(0.9, 1.1)
            )
            impressions = max(200, min(impressions, 400_000))

            campaign_ctr = max(0.0005, ctr * rng.uniform(0.88, 1.12))
            clicks = int(impressions * campaign_ctr)
            campaign_cvr = max(0.001, cvr * rng.uniform(0.85, 1.15))
            conversions = int(clicks * campaign_cvr)
            cost = round(clicks * cpc * rng.uniform(0.95, 1.05), 2)
            revenue = round(conversions * aov * rng.uniform(0.92, 1.08), 2)
            reach = max(1, int(impressions / rng.uniform(1.6, 3.2)))

            per_creative = _split_across_creatives(
                rng,
                len(CREATIVE_COPY[:4]),
                impressions,
                clicks,
                conversions,
                cost,
                revenue,
                quality,
            )
            campaign_creatives = [c for c in creatives if c.campaign_id == campaign.id]

            for creative, share in zip(campaign_creatives, per_creative, strict=False):
                daily_rows.append(
                    DailyMetric(
                        id=new_id("met"),
                        campaign_id=campaign.id,
                        creative_id=creative.id,
                        stat_date=stat_date,
                        impressions=share[0],
                        clicks=share[1],
                        conversions=share[2],
                        cost=share[3],
                        revenue=share[4],
                        unique_reach=max(1, int(share[0] / rng.uniform(1.6, 3.2))),
                    )
                )

            daily_rows.append(
                DailyMetric(
                    id=new_id("met"),
                    campaign_id=campaign.id,
                    creative_id=None,
                    stat_date=stat_date,
                    impressions=impressions,
                    clicks=clicks,
                    conversions=conversions,
                    cost=cost,
                    revenue=revenue,
                    unique_reach=reach,
                )
            )

    session.add_all(campaigns)
    session.add_all(creatives)
    session.add_all(daily_rows)
    await session.flush()

    logger.info(
        "seed_completed",
        campaigns=len(campaigns),
        creatives=len(creatives),
        daily_rows=len(daily_rows),
    )
    return {
        "created_admin": created,
        "campaigns": len(campaigns),
        "creatives": len(creatives),
        "daily_rows": len(daily_rows),
        "skipped": False,
    }


def _split_across_creatives(
    rng: random.Random,
    count: int,
    impressions: int,
    clicks: int,
    conversions: int,
    cost: float,
    revenue: float,
    quality: str,
) -> list[tuple[int, int, int, float, float]]:
    """Distribute a day's totals across creatives with deliberate imbalance."""
    weights = [rng.uniform(0.8, 1.2) for _ in range(count)]
    if quality == "weak":
        # The last creative is a proven loser: high spend, almost no return.
        weights[-1] = weights[-1] * 1.6
    total_weight = sum(weights)

    shares: list[tuple[int, int, int, float, float]] = []
    remaining = [impressions, clicks, conversions, cost, revenue]
    for index, weight in enumerate(weights):
        fraction = weight / total_weight
        row: tuple[int, int, int, float, float]
        if index == count - 1:
            # The last creative absorbs whatever is left over, so the rows sum
            # back to the day totals exactly instead of losing the remainder to
            # rounding.
            row = (
                int(remaining[0]),
                int(remaining[1]),
                int(remaining[2]),
                round(float(remaining[3]), 2),
                round(float(remaining[4]), 2),
            )
        else:
            row_impressions = int(impressions * fraction)
            row_clicks = int(clicks * fraction)
            row_conversions = int(conversions * fraction)
            if quality == "weak" and index == count - 2:
                row_conversions = max(0, row_conversions - 1)
            row_cost = cost * fraction
            row_revenue = revenue * fraction
            if quality == "weak" and index == count - 2:
                row_revenue *= 0.25
            row = (row_impressions, row_clicks, row_conversions, row_cost, row_revenue)
            remaining = [
                remaining[0] - row[0],
                remaining[1] - row[1],
                remaining[2] - row[2],
                remaining[3] - row[3],
                remaining[4] - row[4],
            ]
        shares.append(row)
    return [
        (
            max(0, s[0]),
            max(0, s[1]),
            max(0, s[2]),
            round(max(0.0, s[3]), 2),
            round(max(0.0, s[4]), 2),
        )
        for s in shares
    ]


def _audience_for(platform: Platform) -> str:
    return {
        Platform.GOOGLE: "High-intent search users, 25-44, tier-1 cities",
        Platform.META: "Interest-based prospecting, 22-45, mobile first",
        Platform.TIKTOK: "Short-form video audiences, 18-34, mobile only",
        Platform.MOCK: "Simulated broad audience",
    }[platform]


async def _ensure_admin(
    session: AsyncSession,
    *,
    admin_email: str,
    admin_password: str,
    security: SecuritySettings | None = None,
) -> bool:
    """Create the operator account if it does not exist yet."""
    users = UserRepository(session)
    if await users.by_email(admin_email):
        return False
    await users.create(
        email=admin_email,
        # Hash with the deployment's configured Argon2 parameters rather than the
        # process-wide defaults, otherwise a tuned SECURITY__ARGON2_* is ignored.
        hashed_password=PasswordHasherService(security).hash(admin_password),
        role=Role.ADMIN,
        full_name="Seeded Administrator",
        must_change_password=True,
    )
    return True
