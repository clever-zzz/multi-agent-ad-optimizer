"""Analytical warehouse write path.

The read side lives in ``infra.warehouse``; this package is its counterpart.
"""

from __future__ import annotations

from .models import (
    REASON_NO_CAMPAIGN,
    REASON_NO_EVENT_ID,
    REASON_UNKNOWN_DEVICE,
    REASON_UNKNOWN_EVENT_TYPE,
    REASON_UNKNOWN_GENDER,
    REASON_UNKNOWN_PLATFORM,
    STORABLE_DEVICES,
    STORABLE_EVENT_TYPES,
    STORABLE_GENDERS,
    STORABLE_PLATFORMS,
    AdEvent,
    DailyMetricRow,
    WriteReport,
    daily_row,
    event_row,
    normalise_event,
)
from .sink import (
    REASON_SINK_DISABLED,
    AnalyticsSink,
    ClickHouseSink,
    NullSink,
    build_sink,
)

__all__ = [
    "REASON_NO_CAMPAIGN",
    "REASON_NO_EVENT_ID",
    "REASON_SINK_DISABLED",
    "REASON_UNKNOWN_DEVICE",
    "REASON_UNKNOWN_EVENT_TYPE",
    "REASON_UNKNOWN_GENDER",
    "REASON_UNKNOWN_PLATFORM",
    "STORABLE_DEVICES",
    "STORABLE_EVENT_TYPES",
    "STORABLE_GENDERS",
    "STORABLE_PLATFORMS",
    "AdEvent",
    "AnalyticsSink",
    "ClickHouseSink",
    "DailyMetricRow",
    "NullSink",
    "WriteReport",
    "build_sink",
    "daily_row",
    "event_row",
    "normalise_event",
]
