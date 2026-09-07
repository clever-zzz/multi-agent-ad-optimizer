"""ASGI entrypoint: uvicorn adoptimizer.main:app"""

from __future__ import annotations

from .app import create_app

app = create_app()

__all__ = ["app"]
