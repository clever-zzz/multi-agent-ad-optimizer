"""Integration tests for probes, diagnostics, security headers and throttling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from adoptimizer.app import create_app
from adoptimizer.core.config import ObservabilitySettings, RateLimitSettings, Settings

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, login, make_settings


@asynccontextmanager
async def standalone(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Boot a second app instance with its own settings (used for limiter tests)."""
    app: FastAPI = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http,
    ):
        yield http


class TestLiveness:
    async def test_healthz_needs_no_auth(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    async def test_healthz_is_exempt_from_rate_limiting(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "rl.db").as_posix(),
            rate_limit=RateLimitSettings(
                enabled=True,
                default_requests_per_minute=1,
                burst=1,
                write_requests_per_minute=1,
            ),
        )
        async with standalone(settings) as http:
            for _ in range(5):
                assert (await http.get("/healthz")).status_code == 200


class TestReadiness:
    async def test_readyz_reports_dependencies(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["environment"] == "test"
        assert body["uptime_seconds"] >= 0
        assert set(body["dependencies"]) >= {
            "database",
            "cache",
            "llm",
            "orchestrator",
            "platforms",
        }

    async def test_readyz_shows_the_mock_llm(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/readyz")).json()
        assert body["dependencies"]["llm"]["provider"] == "mock"
        assert body["dependencies"]["llm"]["status"] == "mock"

    async def test_readyz_reports_the_database_dialect(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/readyz")).json()
        assert body["dependencies"]["database"] == {"status": "ok", "dialect": "sqlite"}
        assert body["dependencies"]["cache"]["backend"] == "memory"


class TestMetrics:
    async def test_prometheus_text_format(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# HELP" in response.text or "# TYPE" in response.text

    async def test_request_metrics_are_recorded(self, client: httpx.AsyncClient) -> None:
        await client.post(API + "/auth/login", json={"email": "x@y.z", "password": "nope"})
        body = (await client.get("/metrics")).text
        assert "http_requests_total" in body

    async def test_metrics_can_be_disabled(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "nom.db").as_posix(),
            observability=ObservabilitySettings(metrics_enabled=False, json_logs=False),
        )
        async with standalone(settings) as http:
            assert (await http.get("/metrics")).status_code == 404


class TestSystemInfo:
    async def test_exposes_runtime_facts_without_secrets(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/system/info")).json()
        assert body["environment"] == "test"
        assert body["llm_provider"] == "mock"
        assert body["data_mode"] == "mock"
        assert body["database_dialect"] == "sqlite"
        assert body["cache_backend"]
        assert body["orchestrator_mode"]
        assert "jwt_secret" not in body

    async def test_root_document_discovery(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/")).json()
        assert body["api"] == API
        assert body["docs"] == "/docs"

    async def test_openapi_schema_covers_the_surface(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["openapi"].startswith("3.")
        for path in (
            "/auth/login",
            "/campaigns",
            "/runs",
            "/actions",
            "/alerts",
            "/analytics/overview",
            "/creatives",
            "/admin/system",
            "/admin/tools",
            "/admin/tools/invocations",
        ):
            assert API + path in schema["paths"], path


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "header",
        [
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
            "Content-Security-Policy",
            "Permissions-Policy",
            "Cross-Origin-Opener-Policy",
        ],
    )
    async def test_baseline_headers_present(self, client: httpx.AsyncClient, header: str) -> None:
        assert header in (await client.get("/healthz")).headers

    async def test_frame_options_denies_embedding(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/healthz")).headers["X-Frame-Options"] == "DENY"

    async def test_csp_restricts_script_sources(self, client: httpx.AsyncClient) -> None:
        csp = (await client.get("/healthz")).headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    async def test_request_ids_are_unique(self, client: httpx.AsyncClient) -> None:
        first = (await client.get("/healthz")).headers["X-Request-ID"]
        second = (await client.get("/healthz")).headers["X-Request-ID"]
        assert first and first != second

    async def test_supplied_request_id_is_echoed(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz", headers={"X-Request-ID": "trace-me-123"})
        assert response.headers["X-Request-ID"] == "trace-me-123"

    async def test_oversized_request_id_is_truncated(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz", headers={"X-Request-ID": "x" * 400})
        assert len(response.headers["X-Request-ID"]) == 128


class TestRateLimiting:
    async def test_write_endpoints_are_throttled(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "throttle.db").as_posix(),
            rate_limit=RateLimitSettings(
                enabled=True,
                default_requests_per_minute=1000,
                burst=1000,
                write_requests_per_minute=2,
            ),
        )
        async with standalone(settings) as http:
            statuses = [
                (await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)).status_code for _ in range(4)
            ]
        assert statuses[0] == 200
        assert 429 in statuses

    async def test_throttled_response_is_a_problem_document(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "retry.db").as_posix(),
            rate_limit=RateLimitSettings(
                enabled=True,
                default_requests_per_minute=1000,
                burst=1000,
                write_requests_per_minute=1,
            ),
        )
        async with standalone(settings) as http:
            await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)
            blocked = await login(http, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 0
        assert blocked.headers["content-type"].startswith("application/problem+json")
        body = blocked.json()
        assert body["code"] == "rate_limited"
        assert body["status"] == 429
        assert body["type"].endswith("/rate_limited")

    async def test_read_traffic_is_not_throttled_by_the_write_budget(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "reads.db").as_posix(),
            rate_limit=RateLimitSettings(
                enabled=True,
                default_requests_per_minute=100,
                burst=100,
                write_requests_per_minute=1,
            ),
        )
        async with standalone(settings) as http:
            statuses = [(await http.get("/system/info")).status_code for _ in range(10)]
        assert statuses == [200] * 10
