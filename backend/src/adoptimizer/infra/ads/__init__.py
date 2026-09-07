"""Ad platform adapters.

Every adapter implements AdsPlatformClient so the optimizer never branches on
which network it is talking to. Unsupported operations raise
ExternalServiceError rather than silently succeeding, which is what the demo
version did by returning a fake success payload.
"""

from .base import (
    AdsPlatformClient,
    CampaignDraft,
    CreativeDraft,
    ExecutionResult,
)
from .registry import build_platform_clients

__all__ = [
    "AdsPlatformClient",
    "CampaignDraft",
    "CreativeDraft",
    "ExecutionResult",
    "build_platform_clients",
]
