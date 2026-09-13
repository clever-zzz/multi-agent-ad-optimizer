"""Which feeds this deployment can pull from.

Mirrors ``infra/ads/registry.py`` on purpose: one place that knows every source,
a ``get`` that fails loudly on an unknown name, and a ``status`` that reports
configuration honestly so the diagnostics endpoint and the CLI can answer "would
this work right now" without attempting it.

A source is registered even when it cannot run. ``platform`` is present in a mock
deployment with ``configured: false`` rather than absent, because "installed but
not credentialed" and "not implemented" are different facts and the second one
should never be implied by the first.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...core.errors import NotFoundError
from ...core.logging import get_logger
from ..ads.registry import PlatformRegistry
from .base import MetricSource
from .platform_report import PlatformReportSource
from .synthetic import SyntheticMetricSource

logger = get_logger(__name__)

__all__ = ["MetricSourceRegistry", "build_metric_sources"]


class MetricSourceRegistry:
    """Routes a pull request to the feed named by the caller."""

    def __init__(self, sources: Sequence[MetricSource]) -> None:
        self._sources: dict[str, MetricSource] = {}
        for source in sources:
            if source.name in self._sources:
                msg = "Duplicate metric source name: " + source.name
                raise ValueError(msg)
            self._sources[source.name] = source

    def get(self, name: str) -> MetricSource:
        """Return one source, or 404 with the list of names that do exist."""
        try:
            return self._sources[name]
        except KeyError as exc:
            raise NotFoundError(
                "Unknown metric source: "
                + name
                + ". Available: "
                + (", ".join(sorted(self._sources)) or "none")
            ) from exc

    def names(self) -> list[str]:
        """Every registered source name, sorted for stable output."""
        return sorted(self._sources)

    def configured(self) -> list[str]:
        """Only the sources that could actually be pulled right now."""
        return sorted(name for name, source in self._sources.items() if source.is_configured)

    def status(self) -> dict[str, dict[str, object]]:
        """Configuration state per source, for the diagnostics surface."""
        return {
            name: {"registered": True, "configured": bool(source.is_configured)}
            for name, source in sorted(self._sources.items())
        }

    def __contains__(self, name: object) -> bool:
        return name in self._sources

    def __len__(self) -> int:
        return len(self._sources)


def build_metric_sources(platforms: PlatformRegistry) -> MetricSourceRegistry:
    """Assemble the feeds available in this deployment."""
    sources: list[MetricSource] = [SyntheticMetricSource(), PlatformReportSource(platforms)]
    registry = MetricSourceRegistry(sources)
    logger.info("metric_sources_ready", sources=registry.names(), configured=registry.configured())
    return registry
