"""Shared pytest fixtures.

The whole suite is hermetic: a per-test SQLite file, the mock LLM provider, the
mock ad-platform adapter and an in-memory cache. Nothing reaches the network and
no docker-compose service is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from adoptimizer.app import create_app
from adoptimizer.core.config import (
    AppSettings,
    ClickHouseSettings,
    DatabaseSettings,
    DataMode,
    Environment,
    LLMProvider,
    LLMSettings,
    ObservabilitySettings,
    OptimizationSettings,
    RateLimitSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
)

ADMIN_EMAIL = "admin@adoptimizer.dev"
ADMIN_PASSWORD = "Adm1n!ChangeMe"
API = "/api/v1"

# Long enough to satisfy the production hardening validator, fixed so tokens are
# reproducible across processes.
TEST_JWT_SECRET = "pytest-only-secret-value-0123456789abcdef"


def make_settings(database_url: str, **overrides: Any) -> Settings:
    """Build a deterministic, offline-only settings object."""
    kwargs: dict[str, Any] = {
        "app": AppSettings(
            environment=Environment.TEST,
            cors_allow_origins=["http://testserver"],
        ),
        "database": DatabaseSettings(url=database_url),
        "redis": RedisSettings(enabled=False),
        "clickhouse": ClickHouseSettings(enabled=False),
        "security": SecuritySettings(
            jwt_secret=SecretStr(TEST_JWT_SECRET),
            bootstrap_admin_email=ADMIN_EMAIL,
            bootstrap_admin_password=SecretStr(ADMIN_PASSWORD),
            # Cheapest parameters that still satisfy the field validators; hashing
            # happens on nearly every auth test.
            argon2_time_cost=1,
            argon2_memory_cost_kib=8192,
            argon2_parallelism=1,
        ),
        "rate_limit": RateLimitSettings(enabled=False),
        "llm": LLMSettings(provider=LLMProvider.MOCK, cache_enabled=False),
        "optimization": OptimizationSettings(max_iterations=2, use_convex_solver=False),
        "observability": ObservabilitySettings(json_logs=False, log_level="WARNING"),
        "data_mode": DataMode.MOCK,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings backed by a throwaway SQLite database."""
    database = tmp_path / "adoptimizer-test.db"
    return make_settings("sqlite+aiosqlite:///" + database.as_posix())


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    """A fully started application: container built, schema created, admin seeded."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired straight into the ASGI app (no real socket)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    """POST credentials and return the raw response for status assertions."""
    return await client.post(API + "/auth/login", json={"email": email, "password": password})


async def access_token(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Log in and return just the access token."""
    response = await login(client, email, password)
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def bearer(token: str) -> dict[str, str]:
    """Authorization header for a token."""
    return {"Authorization": "Bearer " + token}


@pytest.fixture
async def admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    """Headers for the bootstrap administrator."""
    return bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


@pytest.fixture
async def make_user(client: httpx.AsyncClient, admin_headers: dict[str, str]):
    """Factory that provisions an account of a given role and returns its headers."""

    async def _make(role: str, password: str = "Operat0r!Passw0rd") -> dict[str, Any]:
        email = role + ".tester@adoptimizer.dev"
        created = await client.post(
            API + "/auth/users",
            json={"email": email, "password": password, "role": role, "full_name": role.title()},
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        token = await access_token(client, email, password)
        return {"email": email, "password": password, "role": role, "headers": bearer(token)}

    return _make


@pytest.fixture
def snapshot_factory():
    """Build PerformanceSnapshot instances without repeating the boilerplate."""
    from adoptimizer.domain.kpi import PerformanceSnapshot

    def _make(
        campaign_id: str,
        *,
        impressions: int = 10_000,
        clicks: int = 300,
        conversions: int = 20,
        total_cost: float = 500.0,
        total_revenue: float = 1_500.0,
        name: str = "",
    ) -> PerformanceSnapshot:
        return PerformanceSnapshot(
            campaign_id=campaign_id,
            campaign_name=name or campaign_id,
            impressions=impressions,
            clicks=clicks,
            conversions=conversions,
            total_cost=total_cost,
            total_revenue=total_revenue,
        )

    return _make
