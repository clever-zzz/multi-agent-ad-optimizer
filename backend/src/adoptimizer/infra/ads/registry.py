"""Adapter registry and routing."""

from __future__ import annotations

from ...core.config import DataMode, get_settings
from ...core.errors import ExternalServiceError
from ...core.logging import get_logger
from ...domain.enums import Platform
from .base import AdsPlatformClient
from .google import build_google_client
from .meta import build_meta_client
from .mock import MockAdsClient
from .tiktok import build_tiktok_client

logger = get_logger(__name__)


class PlatformRegistry:
    """Routes an operation to the adapter for its platform."""

    def __init__(self, clients: dict[Platform, AdsPlatformClient], *, data_mode: DataMode) -> None:
        self._clients = clients
        self._data_mode = data_mode

    @property
    def data_mode(self) -> DataMode:
        return self._data_mode

    def for_platform(self, platform: Platform | str) -> AdsPlatformClient:
        """Return the client for a platform, or the mock when running in mock mode."""
        resolved = Platform(platform) if not isinstance(platform, Platform) else platform
        if self._data_mode == DataMode.MOCK:
            return self._clients[Platform.MOCK]

        client = self._clients.get(resolved)
        if client is None:
            raise ExternalServiceError("No adapter registered for platform " + resolved.value)
        if not client.is_configured:
            raise ExternalServiceError(
                "Platform "
                + resolved.value
                + " is not configured; set its credentials or use mock mode"
            )
        return client

    def status(self) -> dict[str, dict[str, object]]:
        """Report configuration state for the diagnostics endpoint."""
        return {
            platform.value: {
                "registered": True,
                "configured": bool(client.is_configured),
            }
            for platform, client in self._clients.items()
        }

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()


def build_platform_clients(data_mode: DataMode | None = None) -> PlatformRegistry:
    """Build every adapter; unconfigured ones report themselves as such."""
    mode = data_mode or get_settings().data_mode
    clients: dict[Platform, AdsPlatformClient] = {
        Platform.MOCK: MockAdsClient(Platform.MOCK),
        Platform.GOOGLE: build_google_client(),
        Platform.META: build_meta_client(),
        Platform.TIKTOK: build_tiktok_client(),
    }
    configured = [p.value for p, c in clients.items() if c.is_configured]
    logger.info("platform_registry_built", mode=mode.value, configured=configured)
    return PlatformRegistry(clients, data_mode=mode)
