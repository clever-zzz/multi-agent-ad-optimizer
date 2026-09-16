"""Write-side value objects for the analytical warehouse.

``ad_events`` is declared with ``Enum8`` columns, which means ClickHouse rejects
a value outside the declared set rather than coercing it. That makes the
normalisation step below load-bearing instead of cosmetic: a feed reporting a
device of ``"ctv"`` would otherwise fail a whole batch insert with an error that
names the column but not the offending row.

Every event is therefore mapped onto the column types first, and anything that
does not fit is reported by reason rather than silently dropped or quietly
defaulted. ``mock`` is deliberately not storable: the warehouse models the three
real networks, and a mock row in an analytical table would corrupt exactly the
numbers that table exists to make trustworthy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ...domain.enums import EventType

# The sets the ``ad_events`` Enum8 columns declare, as plain frozensets so the
# check is a lookup rather than an exception-driven parse.
STORABLE_PLATFORMS: frozenset[str] = frozenset({"google", "meta", "tiktok"})
STORABLE_DEVICES: frozenset[str] = frozenset({"mobile", "desktop", "tablet"})
STORABLE_GENDERS: frozenset[str] = frozenset({"male", "female", "unknown"})
STORABLE_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in EventType)

# ``device`` has no ``unknown`` member in the existing table, so a missing value
# has to land on the least-surprising member. This is the one place a default is
# applied instead of a rejection, and it is limited to demographic detail - never
# to money, identity or a date.
FALLBACK_DEVICE = "mobile"
FALLBACK_GENDER = "unknown"

# Reasons a row can be refused. Stable strings, because they are counted and
# surfaced; a free-form message would make them impossible to aggregate.
REASON_NO_EVENT_ID = "missing_event_id"
REASON_NO_CAMPAIGN = "missing_campaign_id"
REASON_UNKNOWN_EVENT_TYPE = "event_type_not_modelled"
REASON_UNKNOWN_PLATFORM = "platform_not_modelled"
REASON_UNKNOWN_DEVICE = "device_not_modelled"
REASON_UNKNOWN_GENDER = "gender_not_modelled"


@dataclass(frozen=True, slots=True)
class AdEvent:
    """One delivery event, as an ad network's event stream would report it.

    ``event_id`` is the idempotency key. It is the caller's responsibility to
    make it stable across retries of the same event - a fresh uuid per attempt
    would defeat the deduplication the sink relies on.
    """

    event_id: str
    campaign_id: str
    event_type: EventType
    event_time: datetime
    creative_id: str = ""
    cost: float = 0.0
    revenue: float = 0.0
    platform: str = ""
    device: str = FALLBACK_DEVICE
    country: str = ""
    age_group: str = ""
    gender: str = FALLBACK_GENDER


@dataclass(frozen=True, slots=True)
class DailyMetricRow:
    """One day of delivery for a campaign, optionally broken down by creative.

    This mirrors the primary datastore's ``daily_metrics`` rather than the event
    stream. Platform APIs hand back daily reports, not individual impressions,
    so this is the shape that can actually be populated today - and it is what
    makes the warehouse usable before an event pipeline exists.
    """

    campaign_id: str
    stat_date: date
    impressions: int = 0
    clicks: int = 0
    conversions: int = 0
    cost: float = 0.0
    revenue: float = 0.0
    creative_id: str = ""
    unique_reach: int | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class WriteReport:
    """What a write attempt did, per table.

    ``reasons`` is a mapping rather than a list so repeated causes collapse into
    one entry with a count. An operator reading a report needs to know that 400
    rows failed because of one bad platform, not read 400 identical lines.
    """

    table: str
    written: int = 0
    skipped: int = 0
    reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return self.written + self.skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "attempted": self.attempted,
            "written": self.written,
            "skipped": self.skipped,
            "reasons": dict(self.reasons),
        }


def normalise_event(event: AdEvent) -> tuple[dict[str, Any] | None, str]:
    """Map an event onto the ``ad_events`` column types.

    Returns ``(row, "")`` when the event can be stored, or ``(None, reason)``
    when a value falls outside a column's declared set. Rejecting beats coercing:
    a campaign reported on a platform the warehouse does not model is a fact the
    operator needs, not something to paper over with a default.
    """
    if not event.event_id.strip():
        return None, REASON_NO_EVENT_ID
    if not event.campaign_id.strip():
        return None, REASON_NO_CAMPAIGN

    event_type = str(event.event_type)
    if event_type not in STORABLE_EVENT_TYPES:
        return None, REASON_UNKNOWN_EVENT_TYPE

    platform = event.platform.strip().lower()
    if platform not in STORABLE_PLATFORMS:
        return None, REASON_UNKNOWN_PLATFORM

    device = (event.device or FALLBACK_DEVICE).strip().lower()
    if device not in STORABLE_DEVICES:
        return None, REASON_UNKNOWN_DEVICE

    gender = (event.gender or FALLBACK_GENDER).strip().lower()
    if gender not in STORABLE_GENDERS:
        return None, REASON_UNKNOWN_GENDER

    return (
        {
            "event_id": event.event_id,
            "campaign_id": event.campaign_id,
            "creative_id": event.creative_id or "",
            "event_type": event_type,
            "cost": float(event.cost),
            "revenue": float(event.revenue),
            "platform": platform,
            "device": device,
            "country": event.country or "",
            "age_group": event.age_group or "",
            "gender": gender,
            "event_time": event.event_time,
        },
        "",
    )


def event_row(event: AdEvent) -> dict[str, Any] | None:
    """The storable row for an event, or None when it cannot be stored."""
    row, _reason = normalise_event(event)
    return row


def daily_row(metric: DailyMetricRow) -> dict[str, Any]:
    """Map a daily metric onto the ``campaign_daily_metrics`` column types.

    Nothing here is rejected: the table declares its dimensions as ``String``
    precisely so that an unrecognised platform is stored and can be filtered on
    rather than dropped at the door.
    """
    return {
        "campaign_id": metric.campaign_id,
        "stat_date": metric.stat_date,
        "creative_id": metric.creative_id or "",
        "impressions": int(metric.impressions),
        "clicks": int(metric.clicks),
        "conversions": int(metric.conversions),
        "cost": round(float(metric.cost), 4),
        "revenue": round(float(metric.revenue), 4),
        "unique_reach": metric.unique_reach,
        "source": metric.source or "",
    }
