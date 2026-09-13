"""The ingestion source layer.

A *source* is anything that can assert daily metric rows: a deterministic
synthetic feed for environments with no ad account, and a pull over the existing
platform adapters for deployments that have one. Push feeds - a warehouse export,
a commerce webhook - do not appear here at all; they arrive through the API and
are handled by ``services.ingest`` directly.

Nothing in this package imports ``schemas`` or ``services``, so a feed can be
built and exercised without an HTTP layer or a database in front of it.
"""

from __future__ import annotations

from .base import DAILY_MEASUREMENTS, MetricSource, SourceRecord, SourceTarget
from .platform_report import PlatformReportSource
from .registry import MetricSourceRegistry, build_metric_sources
from .synthetic import SYNTHETIC_SEED, SyntheticMetricSource

__all__ = [
    "DAILY_MEASUREMENTS",
    "SYNTHETIC_SEED",
    "MetricSource",
    "MetricSourceRegistry",
    "PlatformReportSource",
    "SourceRecord",
    "SourceTarget",
    "SyntheticMetricSource",
    "build_metric_sources",
]
