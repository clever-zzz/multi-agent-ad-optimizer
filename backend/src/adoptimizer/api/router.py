"""API composition: one place that knows every router."""

from __future__ import annotations

from fastapi import APIRouter

from ..core.config import get_settings
from .v1 import actions, admin, alerts, analytics, auth, campaigns, creatives, ingest, runs

settings = get_settings()

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(campaigns.router)
api_router.include_router(creatives.router)
api_router.include_router(runs.router)
api_router.include_router(actions.router)
api_router.include_router(alerts.router)
api_router.include_router(analytics.router)
api_router.include_router(ingest.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
