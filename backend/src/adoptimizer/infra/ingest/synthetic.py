"""A deterministic synthetic metric feed.

**This is not advertising data.** It is arithmetic on a seeded RNG, and every row
it produces is stamped ``source="synthetic"`` in ``daily_metrics`` so that the
provenance column, not someone's memory, is what says so. Its job is to exercise
the ingestion path end to end - resolution, upsert, partial columns, batch
accounting - in an environment that has no ad account to read from.

Two properties matter and both are deliberate:

*Deterministic.* Numbers derive from ``(seed, platform, external_id, day)``, so
replaying the same window produces byte-identical rows. A feed whose values
changed between replays would make "created vs updated" meaningless and would be
impossible to write an assertion against.

*Partial on request.* ``columns`` narrows which measurements are asserted. The
default is all of them, imitating a warehouse export that knows revenue too;
dropping ``revenue`` imitates an ad network, and is how the "leave the stored
value alone" path gets tested against something real.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from ...core.logging import get_logger
from .base import DAILY_MEASUREMENTS, SourceRecord, SourceTarget

logger = get_logger(__name__)

# Fixed so two environments pulling the same window agree, and so a test can
# state an expected number without recomputing the generator.
SYNTHETIC_SEED = 20260909

# Per-campaign personality ranges. Chosen to span the same spread as the demo
# seed - some efficient, some bleeding - so downstream rules have work to do.
_IMPRESSION_FLOOR = 4_000
_IMPRESSION_SPAN = 176_000
_CTR_RANGE = (0.008, 0.055)
_CVR_RANGE = (0.015, 0.090)
_CPC_RANGE = (0.35, 2.60)
_AOV_RANGE = (45.0, 320.0)
_REACH_DIVISOR_RANGE = (1.6, 3.2)
_WEEKEND_UPLIFT = 1.15
_DAILY_JITTER = 0.10


class SyntheticMetricSource:
    """Generates plausible daily aggregates for the campaigns it is given."""

    name = "synthetic"

    def __init__(self, *, seed: int = SYNTHETIC_SEED, columns: Iterable[str] | None = None) -> None:
        self._seed = seed
        requested = tuple(columns) if columns is not None else DAILY_MEASUREMENTS
        unknown = [name for name in requested if name not in DAILY_MEASUREMENTS]
        if unknown:
            msg = "Unknown measurement columns: " + ", ".join(sorted(unknown))
            raise ValueError(msg)
        self._columns = frozenset(requested)
        logger.info(
            "synthetic_source_ready",
            seed=seed,
            columns=sorted(self._columns),
            notice="generated numbers, not advertising data",
        )

    @property
    def is_configured(self) -> bool:
        """Nothing external to configure; the source is always available."""
        return True

    @property
    def columns(self) -> frozenset[str]:
        """Which measurements this instance asserts."""
        return self._columns

    async def fetch(
        self, *, start: date, end: date, targets: Sequence[SourceTarget] = ()
    ) -> list[SourceRecord]:
        """One record per target per day in the closed interval ``[start, end]``.

        Unlike a platform feed this source cannot enumerate campaigns on its own,
        so with no targets it has nothing to say and returns an empty list rather
        than inventing campaigns that do not exist.
        """
        if end < start:
            msg = "end precedes start: " + start.isoformat() + " > " + end.isoformat()
            raise ValueError(msg)

        records: list[SourceRecord] = []
        for target in targets:
            profile = self._profile(target)
            day = start
            while day <= end:
                records.append(self._record(target, day, profile))
                day += timedelta(days=1)
        return records

    def _profile(self, target: SourceTarget) -> dict[str, float]:
        """Stable per-campaign characteristics derived from its identity."""
        rng = random.Random(self._key(target.external_id, "profile"))  # noqa: S311
        return {
            "impressions": float(
                rng.randint(_IMPRESSION_FLOOR, _IMPRESSION_FLOOR + _IMPRESSION_SPAN)
            ),
            "ctr": rng.uniform(*_CTR_RANGE),
            "cvr": rng.uniform(*_CVR_RANGE),
            "cpc": rng.uniform(*_CPC_RANGE),
            "aov": rng.uniform(*_AOV_RANGE),
        }

    def _record(self, target: SourceTarget, day: date, profile: dict[str, float]) -> SourceRecord:
        rng = random.Random(self._key(target.external_id, day.isoformat()))  # noqa: S311
        uplift = _WEEKEND_UPLIFT if day.weekday() >= 5 else 1.0
        jitter = 1.0 + rng.uniform(-_DAILY_JITTER, _DAILY_JITTER)

        impressions = max(1, int(profile["impressions"] * uplift * jitter))
        clicks = int(impressions * max(0.0005, profile["ctr"] * rng.uniform(0.9, 1.1)))
        conversions = int(clicks * max(0.001, profile["cvr"] * rng.uniform(0.9, 1.1)))
        cost = round(clicks * profile["cpc"] * rng.uniform(0.95, 1.05), 2)
        revenue = round(conversions * profile["aov"] * rng.uniform(0.92, 1.08), 2)
        reach = max(1, int(impressions / rng.uniform(*_REACH_DIVISOR_RANGE)))

        # Every column is computed before any is dropped, so narrowing `columns`
        # changes which measurements are asserted without shifting the RNG
        # sequence behind the ones that remain. Otherwise two instances of this
        # source would disagree about impressions as well as about revenue.
        def kept(name: str) -> bool:
            return name in self._columns

        return SourceRecord(
            stat_date=day,
            platform=target.platform,
            external_id=target.external_id,
            campaign_id=target.campaign_id,
            # None means "this feed does not measure that column", which the
            # writer honours by leaving the stored value alone. Zero would mean
            # "it measured nothing happened", and would overwrite the truth.
            impressions=impressions if kept("impressions") else None,
            clicks=clicks if kept("clicks") else None,
            conversions=conversions if kept("conversions") else None,
            cost=cost if kept("cost") else None,
            revenue=revenue if kept("revenue") else None,
            unique_reach=reach if kept("unique_reach") else None,
        )

    def _key(self, external_id: str, salt: str) -> str:
        return str(self._seed) + "|" + external_id + "|" + salt
