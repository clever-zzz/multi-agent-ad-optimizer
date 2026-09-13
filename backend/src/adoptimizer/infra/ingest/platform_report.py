"""Pull source backed by the ad-platform adapters.

This is the socket the ingestion framework is built around: the same
``AdsPlatformClient.fetch_report`` the diagnostic probe uses, turned into
``SourceRecord`` rows. All three adapters normalise to one shape -
``{date, impressions, clicks, conversions, cost}`` - so this module has nothing
platform-specific in it and a fourth network only has to match that shape.

Two things it does not do, on purpose:

*It never reports revenue.* No ad network knows your revenue. The column is left
as ``None``, which the writer reads as "not measured" and leaves alone. Writing
0 would silently erase every revenue figure a commerce feed had supplied and the
optimizer would then optimise against a fabricated ROAS of zero.

*It never swallows a failure.* An unconfigured adapter raises out of the registry
and stays raised, because "the feed errored" and "the feed had nothing to report"
are different facts and an operator must be able to tell them apart at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from ...core.config import DataMode
from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ..ads.registry import PlatformRegistry
from .base import SourceRecord, SourceTarget

logger = get_logger(__name__)

__all__ = ["PlatformReportSource"]


class PlatformReportSource:
    """Reads daily campaign reports from whichever adapters are configured."""

    name = "platform"

    def __init__(self, registry: PlatformRegistry) -> None:
        self._registry = registry

    @property
    def is_configured(self) -> bool:
        """True only when a real adapter could actually be reached.

        Mock mode is reported as unconfigured rather than allowed to return an
        empty list: ``MockAdsClient.fetch_report`` has no rows by construction,
        so a successful-looking pull that produced nothing would be the least
        useful possible answer.
        """
        if self._registry.data_mode != DataMode.WAREHOUSE:
            return False
        return any(
            entry.get("configured")
            for entry in self._registry.status().values()
            if entry.get("registered")
        )

    async def fetch(
        self, *, start: date, end: date, targets: Sequence[SourceTarget] = ()
    ) -> list[SourceRecord]:
        """One report call per target campaign, flattened into daily records."""
        if end < start:
            msg = "end precedes start: " + start.isoformat() + " > " + end.isoformat()
            raise ValueError(msg)
        if not self.is_configured:
            raise ExternalServiceError(
                "The platform report source needs DATA_MODE=warehouse and at least one "
                "configured adapter; run `adoptimizer creds` to see what is missing"
            )

        records: list[SourceRecord] = []
        skipped = 0
        for target in targets:
            client = self._registry.for_platform(target.platform)
            payload = await client.fetch_report(
                target.external_id, start_date=start.isoformat(), end_date=end.isoformat()
            )
            for row in payload.get("rows") or []:
                record = self._to_record(target, row)
                if record is None:
                    skipped += 1
                    continue
                records.append(record)

        if skipped:
            logger.warning(
                "platform_report_rows_skipped",
                skipped=skipped,
                reason="row carried no parseable date",
                source=self.name,
            )
        return records

    def _to_record(self, target: SourceTarget, row: Any) -> SourceRecord | None:
        """Convert one normalised platform row, or None when it has no usable date."""
        if not isinstance(row, dict):
            return None
        parsed = _parse_stat_date(row.get("date"))
        if parsed is None:
            return None
        return SourceRecord(
            stat_date=parsed,
            platform=target.platform,
            external_id=target.external_id,
            campaign_id=target.campaign_id,
            impressions=_as_int(row.get("impressions")),
            clicks=_as_int(row.get("clicks")),
            conversions=_as_int(row.get("conversions")),
            cost=_as_float(row.get("cost")),
            # Not measured by an ad network; see the module docstring.
            revenue=None,
            unique_reach=None,
        )


def _parse_stat_date(value: Any) -> date | None:
    """Read a platform date, tolerating the timestamp form TikTok returns.

    ``stat_time_day`` arrives as "2026-09-09 00:00:00" rather than an ISO date,
    and ``date.fromisoformat`` rejects the whole string on the 3.12 floor. Taking
    the leading ten characters is enough for both shapes.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value.strip()) < 10:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None
