"""The warehouse CLI: the operator's entry point to the analytical write path.

Kept in its own module rather than added to ``test_cli.py`` so it can be run on
its own - the serve tests in that file start a uvicorn subprocess and are
excluded from local runs.

The behaviour under test is the exit code, not just the printed JSON. These
commands exist to be called from a deployment script or a CronJob, and the only
thing such a caller can act on is whether the command failed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adoptimizer.cli import app
from adoptimizer.core.config import get_settings
from adoptimizer.core.errors import ExternalServiceError
from adoptimizer.infra import analytics
from adoptimizer.infra.analytics import DailyMetricRow, WriteReport

runner = CliRunner()


class AvailableSink:
    """A sink that accepts writes, standing in for a reachable ClickHouse."""

    name = "fake"
    is_available = True

    def __init__(self) -> None:
        self.rows: list[DailyMetricRow] = []

    async def write_events(self, events: Sequence[Any]) -> WriteReport:
        _ = events
        return WriteReport(table="ad_events")

    async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport:
        self.rows.extend(rows)
        return WriteReport(table="campaign_daily_metrics", written=len(rows))

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "status": "ok"}

    async def close(self) -> None:
        return None


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway database and drop the settings cache."""
    database = tmp_path / "cli_warehouse.db"
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


def seed() -> None:
    """Populate the portfolio the mirror reads from."""
    assert runner.invoke(app, ["seed"]).exit_code == 0


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> AvailableSink:
    """Replace the sink factory with one that always accepts writes."""
    instance = AvailableSink()

    async def fake_build_sink(settings: Any = None) -> AvailableSink:
        _ = settings
        return instance

    monkeypatch.setattr(analytics, "build_sink", fake_build_sink)
    return instance


class TestWarehouseStatus:
    def test_a_disabled_sink_is_reported_and_the_command_fails(self, cli_env: Path) -> None:
        """A pre-flight check in a deploy script must fail, not skip silently."""
        result = runner.invoke(app, ["warehouse", "status"])

        assert result.exit_code == 1, result.output
        body = payload(result.output)
        assert body["sink"] == "null"
        assert body["available"] is False
        assert body["health"]["status"] == "disabled"

    def test_a_reachable_sink_succeeds(self, cli_env: Path, sink: AvailableSink) -> None:
        result = runner.invoke(app, ["warehouse", "status"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["sink"] == "fake"
        assert body["available"] is True


class TestWarehouseSync:
    def test_a_dry_run_reports_the_volume_without_writing(
        self, cli_env: Path, sink: AvailableSink
    ) -> None:
        seed()

        result = runner.invoke(app, ["warehouse", "sync", "--days", "30", "--dry-run"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["dry_run"] is True
        assert body["candidates"] > 0
        assert body["warehouse"]["written"] == 0
        assert sink.rows == []

    def test_a_sync_mirrors_the_seeded_portfolio(self, cli_env: Path, sink: AvailableSink) -> None:
        seed()

        result = runner.invoke(app, ["warehouse", "sync", "--days", "30"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["ok"] is True
        assert body["warehouse"]["written"] == body["candidates"]
        assert len(sink.rows) == body["candidates"]

    def test_rows_found_but_none_landed_exits_non_zero(self, cli_env: Path) -> None:
        """A disabled warehouse over a populated window is the failure to catch."""
        seed()

        result = runner.invoke(app, ["warehouse", "sync", "--days", "30"])

        assert result.exit_code == 1, result.output
        body = payload(result.output)
        assert body["candidates"] > 0
        assert body["warehouse"]["written"] == 0
        assert body["ok"] is False

    def test_a_sink_failure_exits_non_zero(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A warehouse that rejects the batch must not look like a clean run."""
        seed()

        class FailingSink(AvailableSink):
            async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport:
                _ = rows
                raise ExternalServiceError("warehouse rejected the batch")

        async def fake_build_sink(settings: Any = None) -> FailingSink:
            _ = settings
            return FailingSink()

        monkeypatch.setattr(analytics, "build_sink", fake_build_sink)

        result = runner.invoke(app, ["warehouse", "sync", "--days", "30"])

        assert result.exit_code != 0

    def test_the_mirror_can_be_scoped_to_one_campaign(
        self, cli_env: Path, sink: AvailableSink
    ) -> None:
        seed()

        result = runner.invoke(
            app, ["warehouse", "sync", "--days", "30", "--campaign", "cmp_does_not_exist"]
        )

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["candidates"] == 0
        assert sink.rows == []
