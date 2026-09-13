"""What a metric feed is, and what it hands us.

Deliberately free of Pydantic and of the API schemas: ``infra`` does not import
``schemas`` anywhere in this codebase, and keeping that true means a feed can be
exercised without an HTTP layer in front of it. The service converts
``SourceRecord`` into the wire contract; nothing else does.

Every measurement is optional. That is not laxness, it is the shape of the
problem: an ad network knows impressions, clicks and cost but has never heard of
your revenue, while a commerce webhook knows revenue and conversions but not
reach. A feed that cannot measure a column must say so with ``None``, which means
"leave the stored value alone". Reporting 0 instead would overwrite a real number
with a lie, and the optimizer would then act on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from ...domain.enums import Platform
from ...domain.kpi import DAILY_MEASUREMENTS

__all__ = ["DAILY_MEASUREMENTS", "MetricSource", "SourceRecord", "SourceTarget"]


@dataclass(frozen=True, slots=True)
class SourceTarget:
    """One campaign a pull source may fetch rows for.

    ``external_id`` is what the platform recognises; ``campaign_id`` is what this
    database recognises. A source that has to ask a platform for data needs the
    first, and a source that is imitating a feed needs both so its rows can be
    resolved without a lookup it cannot perform.
    """

    platform: Platform
    external_id: str
    campaign_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One daily aggregate asserted by a feed.

    Identity is expressed the way the feed naturally has it - platform plus
    external id - or by internal id. The service resolves either; a source does
    not need database access to produce a record.
    """

    stat_date: date
    platform: Platform | None = None
    external_id: str | None = None
    campaign_id: str | None = None
    creative_id: str | None = None
    impressions: int | None = None
    clicks: int | None = None
    conversions: int | None = None
    cost: float | None = None
    revenue: float | None = None
    unique_reach: int | None = None

    def measurements(self) -> dict[str, Any]:
        """Only the columns this feed actually asserted."""
        return {
            name: value for name in DAILY_MEASUREMENTS if (value := getattr(self, name)) is not None
        }

    def identity(self) -> str:
        """A short, log-safe description of what this record points at."""
        if self.campaign_id:
            return "campaign_id=" + self.campaign_id
        if self.platform is not None and self.external_id:
            return self.platform.value + "/" + self.external_id
        return "unaddressed"


class MetricSource(Protocol):
    """A feed that can be pulled.

    ``is_configured`` exists so an operator can ask "would this source work
    right now" without triggering a call, and so the registry can report honest
    status instead of failing at fetch time.

    ``targets`` is advisory. A real platform feed is the authority on which
    campaigns exist and may ignore it entirely; a source that has nothing of its
    own to enumerate - the synthetic one, or a backfill - depends on it.
    """

    name: str

    @property
    def is_configured(self) -> bool:
        """Whether a pull could succeed right now.

        Declared read-only so an implementation can derive it - the platform
        source computes it from the adapter registry on every access - rather
        than being forced to cache a value that goes stale when credentials
        arrive.
        """
        ...

    async def fetch(
        self, *, start: date, end: date, targets: Sequence[SourceTarget] = ()
    ) -> list[SourceRecord]:
        """Return every row this feed asserts for the closed interval."""
        ...
