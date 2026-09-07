"""The operator command line.

Incident response runs through these commands, so each one is asserted on its
exit code and its machine-readable output: a CLI that prints prose or exits 0 on
failure cannot be used in a deploy script or an alert runbook.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adoptimizer.cli import app
from adoptimizer.core.config import get_settings
from adoptimizer.domain.enums import RunStatus

runner = CliRunner()

COMMANDS = ("serve", "migrate", "revision", "seed", "run", "healthcheck", "token")


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway database and drop the settings cache."""
    database = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///" + database.as_posix())
    monkeypatch.setenv("APP__ENVIRONMENT", "test")
    monkeypatch.setenv("LLM__PROVIDER", "mock")
    monkeypatch.setenv("DATA_MODE", "mock")
    monkeypatch.setenv("REDIS__ENABLED", "false")
    monkeypatch.setenv("CLICKHOUSE__ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY__JSON_LOGS", "false")
    monkeypatch.setenv("OBSERVABILITY__LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield database
    get_settings.cache_clear()


def payload(output: str) -> Any:
    """Parse the trailing JSON document a command printed."""
    start = output.index("{")
    return json.loads(output[start:])


class TestDiscovery:
    def test_the_help_lists_every_operator_command(self) -> None:
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        for command in COMMANDS:
            assert command in result.output

    def test_no_arguments_prints_help_instead_of_guessing(self) -> None:
        result = runner.invoke(app, [])

        assert "Usage" in result.output
        # Click's usage-error convention: help on stdout, exit code 2.
        assert result.exit_code == 2

    def test_an_unknown_command_is_refused(self) -> None:
        result = runner.invoke(app, ["deploy"])

        assert result.exit_code != 0


class TestServe:
    def test_the_bind_options_reach_uvicorn(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_run(target: str, **kwargs: Any) -> None:
            captured["target"] = target
            captured.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)

        result = runner.invoke(
            app, ["serve", "--host", "127.0.0.1", "--port", "9001", "--workers", "4"]
        )

        assert result.exit_code == 0, result.output
        assert captured["target"] == "adoptimizer.main:app"
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 9001
        assert captured["workers"] == 4
        assert captured["reload"] is False

    def test_reload_forces_a_single_worker(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uvicorn rejects --workers with --reload; the CLI must not pass both."""
        captured: dict[str, Any] = {}

        def fake_run(target: str, **kwargs: Any) -> None:
            _ = target
            captured.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)

        result = runner.invoke(app, ["serve", "--reload", "--workers", "4"])

        assert result.exit_code == 0, result.output
        assert captured["reload"] is True
        assert captured["workers"] == 1

    def test_logging_is_left_to_the_application(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uvicorn's own log config would discard the structured request context."""
        captured: dict[str, Any] = {}

        def fake_run(target: str, **kwargs: Any) -> None:
            _ = target
            captured.update(kwargs)

        monkeypatch.setattr("uvicorn.run", fake_run)

        assert runner.invoke(app, ["serve"]).exit_code == 0
        assert captured["log_config"] is None
        assert captured["proxy_headers"] is True
        assert captured["forwarded_allow_ips"] == "*"


class TestMigrations:
    @pytest.fixture
    def alembic(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Record what the CLI asks Alembic to do, without touching a database."""
        calls: dict[str, Any] = {"upgrade": [], "revision": [], "config": None}

        class StubConfig:
            def __init__(self, path: str) -> None:
                self.path = path
                self.options: dict[str, str] = {}

            def set_main_option(self, key: str, value: str) -> None:
                self.options[key] = value

        def make_config(path: str) -> StubConfig:
            calls["config"] = StubConfig(path)
            return calls["config"]

        def upgrade(config: Any, revision: str, **kwargs: Any) -> None:
            calls["upgrade"].append({"config": config, "revision": revision, **kwargs})

        def revision(config: Any, **kwargs: Any) -> None:
            calls["revision"].append({"config": config, **kwargs})

        monkeypatch.setattr("alembic.config.Config", make_config)
        monkeypatch.setattr("alembic.command.upgrade", upgrade)
        monkeypatch.setattr("alembic.command.revision", revision)
        return calls

    def test_migrate_applies_the_head_revision(
        self, cli_env: Path, alembic: dict[str, Any]
    ) -> None:
        result = runner.invoke(app, ["migrate"])

        assert result.exit_code == 0, result.output
        assert "Migrations applied up to head" in result.output
        assert alembic["upgrade"][0]["revision"] == "head"
        assert "sql" not in alembic["upgrade"][0]

    def test_the_async_driver_is_stripped_for_alembic(
        self, cli_env: Path, alembic: dict[str, Any]
    ) -> None:
        """Alembic runs synchronously; an aiosqlite URL would fail to connect."""
        runner.invoke(app, ["migrate"])

        url = alembic["config"].options["sqlalchemy.url"]
        assert "+aiosqlite" not in url
        assert url.startswith("sqlite:///")

    def test_migrate_can_emit_sql_without_applying_it(
        self, cli_env: Path, alembic: dict[str, Any]
    ) -> None:
        result = runner.invoke(app, ["migrate", "--offline", "--revision", "0001"])

        assert result.exit_code == 0, result.output
        assert alembic["upgrade"][0]["sql"] is True
        assert alembic["upgrade"][0]["revision"] == "0001"

    def test_revision_autogenerates_from_the_orm(
        self, cli_env: Path, alembic: dict[str, Any]
    ) -> None:
        result = runner.invoke(app, ["revision", "--message", "add spend cap"])

        assert result.exit_code == 0, result.output
        assert alembic["revision"][0]["message"] == "add spend cap"
        assert alembic["revision"][0]["autogenerate"] is True

    def test_a_message_is_required_for_a_new_revision(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["revision"])

        assert result.exit_code != 0


class TestSeed:
    def test_seed_builds_the_demo_portfolio_and_the_admin(self, cli_env: Path) -> None:
        result = runner.invoke(app, ["seed"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["created_admin"] is True
        assert body["campaigns"] == 8
        assert body["creatives"] == 32
        assert body["daily_rows"] > 0
        assert body["skipped"] is False
        assert cli_env.exists()

    def test_seeding_twice_reports_that_it_skipped(self, cli_env: Path) -> None:
        assert runner.invoke(app, ["seed"]).exit_code == 0

        second = runner.invoke(app, ["seed"])

        assert second.exit_code == 0, second.output
        assert payload(second.output)["skipped"] is True


class TestRunOptimization:
    class StubRun:
        def __init__(self, run_id: str, status: RunStatus, summary: dict[str, Any]) -> None:
            self.id = run_id
            self.status = status
            self.summary = summary

    def stub_service(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Replace the optimization service so the CLI is tested, not the loop."""
        calls: dict[str, Any] = {"start": [], "waited": [], "detail": []}
        outer = self

        class StubOptimizationService:
            def __init__(self, container: Any, session: Any) -> None:
                _ = container, session

            async def start_run(self, session: Any, **kwargs: Any) -> Any:
                _ = session
                calls["start"].append(kwargs)
                return outer.StubRun("run_cli_1", RunStatus.PENDING, {})

            async def wait_for(
                self,
                run_id: str,
                *,
                # A test double has to mirror OptimizationService.wait_for exactly.
                timeout: float = 300.0,  # noqa: ASYNC109
            ) -> bool:
                calls["waited"].append({"run_id": run_id, "timeout": timeout})
                return True

            async def run_detail(self, session: Any, run_id: str) -> dict[str, Any]:
                _ = session
                calls["detail"].append(run_id)
                return {
                    "run": outer.StubRun(
                        run_id, RunStatus.SUCCEEDED, {"actions": 12, "alerts_raised": 3}
                    )
                }

        monkeypatch.setattr(
            "adoptimizer.services.optimization.OptimizationService", StubOptimizationService
        )
        return calls

    def test_run_waits_and_prints_the_summary(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self.stub_service(monkeypatch)

        result = runner.invoke(app, ["run", "--max-iterations", "3", "--window-days", "14"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["run_id"] == "run_cli_1"
        assert body["completed"] is True
        assert body["status"] == "succeeded"
        assert body["summary"] == {"actions": 12, "alerts_raised": 3}
        assert calls["start"][0]["trigger_type"] == "cli"
        assert calls["start"][0]["max_iterations"] == 3
        assert calls["start"][0]["window_days"] == 14
        assert calls["start"][0]["background"] is True
        assert calls["waited"] == [{"run_id": "run_cli_1", "timeout": 600.0}]

    def test_campaign_filters_are_passed_through(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self.stub_service(monkeypatch)

        result = runner.invoke(
            app, ["run", "--campaign", "camp_a", "--campaign", "camp_b", "--no-wait"]
        )

        assert result.exit_code == 0, result.output
        assert calls["start"][0]["campaign_ids"] == ["camp_a", "camp_b"]

    def test_no_wait_dispatches_without_blocking(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cron job must be able to queue a run and exit immediately."""
        calls = self.stub_service(monkeypatch)

        result = runner.invoke(app, ["run", "--no-wait"])

        assert result.exit_code == 0, result.output
        assert payload(result.output) == {"run_id": "run_cli_1", "status": "dispatched"}
        assert calls["waited"] == []
        assert calls["detail"] == []


class TestHealthcheck:
    class StubResponse:
        def __init__(self, status_code: int, body: dict[str, Any]) -> None:
            self.status_code = status_code
            self._body = body
            self.text = json.dumps(body)

        def json(self) -> dict[str, Any]:
            return self._body

    def patch_http(self, monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> list[str]:
        seen: list[str] = []

        def fake_get(url: str, timeout: float = 5.0) -> Any:
            _ = timeout
            seen.append(url)
            outcome = responses[url.rsplit("/", 1)[-1]]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(httpx, "get", fake_get)
        return seen

    def test_a_ready_instance_exits_zero(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self.patch_http(
            monkeypatch,
            {
                "healthz": self.StubResponse(200, {"status": "alive"}),
                "readyz": self.StubResponse(200, {"status": "ready"}),
            },
        )

        result = runner.invoke(app, ["healthcheck", "--url", "http://api.internal:8000"])

        assert result.exit_code == 0, result.output
        assert seen == ["http://api.internal:8000/healthz", "http://api.internal:8000/readyz"]
        assert payload(result.output)["readiness_status"] == 200

    def test_an_unready_instance_exits_non_zero(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deploy script must be able to fail the rollout on this."""
        self.patch_http(
            monkeypatch,
            {
                "healthz": self.StubResponse(200, {"status": "alive"}),
                "readyz": self.StubResponse(503, {"status": "unavailable"}),
            },
        )

        result = runner.invoke(app, ["healthcheck"])

        assert result.exit_code == 1
        assert payload(result.output)["readiness_status"] == 503

    def test_an_unreachable_instance_exits_two(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A distinct code separates "down" from "up but unhealthy"."""
        self.patch_http(monkeypatch, {"healthz": httpx.ConnectError("refused"), "readyz": None})

        result = runner.invoke(app, ["healthcheck"])

        assert result.exit_code == 2
        assert "Unreachable" in result.output


class TestToken:
    def test_a_successful_login_prints_the_token(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_post(url: str, json: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
            _ = timeout
            captured["url"] = url
            captured["body"] = json
            return TestHealthcheck.StubResponse(
                200, {"access_token": "tok_abc", "token_type": "bearer", "expires_in": 1800}
            )

        monkeypatch.setattr(httpx, "post", fake_post)

        result = runner.invoke(
            app, ["token", "--email", "admin@adoptimizer.dev", "--password", "Adm1n!ChangeMe"]
        )

        assert result.exit_code == 0, result.output
        assert captured["url"].endswith("/api/v1/auth/login")
        assert captured["body"] == {
            "email": "admin@adoptimizer.dev",
            "password": "Adm1n!ChangeMe",
        }
        assert payload(result.output)["access_token"] == "tok_abc"

    def test_a_failed_login_exits_non_zero_and_shows_the_reason(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_post(url: str, json: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
            _ = url, json, timeout
            return TestHealthcheck.StubResponse(401, {"detail": "invalid credentials"})

        monkeypatch.setattr(httpx, "post", fake_post)

        result = runner.invoke(
            app, ["token", "--email", "admin@adoptimizer.dev", "--password", "wrong"]
        )

        assert result.exit_code == 1
        assert "invalid credentials" in result.output

    def test_the_password_can_be_prompted_for(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operators should not have to put a password in shell history."""
        seen: dict[str, Any] = {}

        def fake_post(url: str, json: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
            _ = url, timeout
            seen["body"] = json
            return TestHealthcheck.StubResponse(200, {"access_token": "tok_prompted"})

        monkeypatch.setattr(httpx, "post", fake_post)

        result = runner.invoke(
            app, ["token", "--email", "admin@adoptimizer.dev"], input="Adm1n!ChangeMe\n"
        )

        assert result.exit_code == 0, result.output
        assert seen["body"]["password"] == "Adm1n!ChangeMe"
        assert "Adm1n!ChangeMe" not in result.output
