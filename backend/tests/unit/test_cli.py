"""The operator command line.

Incident response runs through these commands, so each one is asserted on its
exit code and its machine-readable output: a CLI that prints prose or exits 0 on
failure cannot be used in a deploy script or an alert runbook.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adoptimizer.cli import _mask, app
from adoptimizer.core.clock import utc_today
from adoptimizer.core.config import DataMode, dotenv_path, get_settings, load_environment
from adoptimizer.core.errors import ExternalServiceError, NotFoundError
from adoptimizer.domain.enums import Platform, RunStatus
from adoptimizer.infra.ads.registry import PlatformRegistry, build_platform_clients
from adoptimizer.infra.ingest import MetricSourceRegistry, SourceRecord, SourceTarget

runner = CliRunner()

COMMANDS = (
    "serve",
    "migrate",
    "revision",
    "seed",
    "ingest",
    "scheduler",
    "run",
    "healthcheck",
    "token",
    "creds",
)


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


class TestIngest:
    """The scheduled data path.

    Asserted against the seeded portfolio, whose 8 campaigns and 21 days of
    history are fixed, so the expected counts are arithmetic rather than a
    snapshot that silently drifts.
    """

    def seed(self) -> None:
        assert runner.invoke(app, ["seed"]).exit_code == 0

    def test_a_dry_run_reports_without_writing(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["ingest", "--source", "synthetic", "--days", "3", "--dry-run"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["dry_run"] is True
        assert body["source"] == "synthetic"
        assert body["received"] == 8 * 3
        assert body["updated"] == 8 * 3
        assert body["created"] == 0
        assert body["batch_id"].startswith("ing_")

    def test_the_days_the_seed_does_not_cover_are_created(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["ingest", "--source", "synthetic", "--days", "25"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["received"] == 8 * 25
        assert body["updated"] == 8 * 21
        assert body["created"] == 8 * 4
        assert body["dry_run"] is False

    def test_a_second_run_updates_what_the_first_created(self, cli_env: Path) -> None:
        self.seed()
        assert runner.invoke(app, ["ingest", "--days", "25"]).exit_code == 0

        second = runner.invoke(app, ["ingest", "--days", "25"])

        assert second.exit_code == 0, second.output
        body = payload(second.output)
        assert body["created"] == 0
        assert body["updated"] == 8 * 25

    def test_an_explicit_window_overrides_the_day_count(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(
            app, ["ingest", "--days", "99", "--start", "2026-01-01", "--end", "2026-01-02"]
        )

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["received"] == 8 * 2
        assert (body["window_start"], body["window_end"]) == ("2026-01-01", "2026-01-02")
        # Two years of history the seed never wrote.
        assert body["created"] == 8 * 2

    def test_an_unknown_feed_is_refused_by_name(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["ingest", "--source", "google"])

        assert result.exit_code != 0
        assert isinstance(result.exception, NotFoundError)

    def test_the_platform_feed_refuses_to_run_in_mock_mode(self, cli_env: Path) -> None:
        """It has no rows to give, so it must fail rather than report a clean zero."""
        self.seed()

        result = runner.invoke(app, ["ingest", "--source", "platform"])

        assert result.exit_code != 0
        assert isinstance(result.exception, ExternalServiceError)

    def test_a_json_batch_can_be_pushed_from_a_file(self, cli_env: Path, tmp_path: Path) -> None:
        self.seed()
        export = tmp_path / "backfill.json"
        export.write_text(
            json.dumps(
                {
                    "source": "backfill.file",
                    "records": [
                        {
                            "platform": "google",
                            "external_id": "ext_google_1000",
                            "stat_date": utc_today().isoformat(),
                            "cost": 12.5,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["ingest", "--file", str(export)])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        # The file names its own feed; --source is only the fallback.
        assert body["source"] == "backfill.file"
        assert body["created"] + body["updated"] == 1

    def test_a_bare_list_of_records_takes_the_source_from_the_flag(
        self, cli_env: Path, tmp_path: Path
    ) -> None:
        self.seed()
        export = tmp_path / "rows.json"
        export.write_text(
            json.dumps(
                [
                    {
                        "platform": "google",
                        "external_id": "ext_google_1000",
                        "stat_date": utc_today().isoformat(),
                        "clicks": 7,
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["ingest", "--source", "rows.file", "--file", str(export)])

        assert result.exit_code == 0, result.output
        assert payload(result.output)["source"] == "rows.file"

    def test_a_file_can_ask_for_a_dry_run_itself(self, cli_env: Path, tmp_path: Path) -> None:
        self.seed()
        export = tmp_path / "rehearsal.json"
        export.write_text(
            json.dumps(
                {
                    "source": "rehearsal.file",
                    "dry_run": True,
                    "records": [
                        {
                            "platform": "google",
                            "external_id": "ext_google_1000",
                            "stat_date": utc_today().isoformat(),
                            "clicks": 7,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["ingest", "--file", str(export)])

        assert result.exit_code == 0, result.output
        assert payload(result.output)["dry_run"] is True

    def test_a_batch_that_lands_nothing_exits_non_zero(self, cli_env: Path, tmp_path: Path) -> None:
        """So a cron job alerting on exit status notices a feed that stopped matching."""
        self.seed()
        export = tmp_path / "orphan.json"
        export.write_text(
            json.dumps(
                [
                    {
                        "platform": "google",
                        "external_id": "ext_never_imported",
                        "stat_date": utc_today().isoformat(),
                        "clicks": 7,
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["ingest", "--file", str(export)])

        assert result.exit_code == 1, result.output
        body = payload(result.output)
        assert body["received"] == 1
        assert body["unresolved_count"] == 1

    def test_a_partially_dirty_batch_still_exits_zero(self, cli_env: Path, tmp_path: Path) -> None:
        """Losing one row is worth reporting but not worth failing a cron job over."""
        self.seed()
        export = tmp_path / "mixed.json"
        export.write_text(
            json.dumps(
                [
                    {
                        "platform": "google",
                        "external_id": "ext_google_1000",
                        "stat_date": utc_today().isoformat(),
                        "clicks": 7,
                    },
                    {
                        "platform": "google",
                        "external_id": "ext_never_imported",
                        "stat_date": utc_today().isoformat(),
                        "clicks": 7,
                    },
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["ingest", "--file", str(export)])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["created"] + body["updated"] == 1
        assert body["unresolved_count"] == 1

    def test_an_empty_feed_exits_zero(self, cli_env: Path) -> None:
        """Nothing to pull is a normal day, not an incident."""
        result = runner.invoke(app, ["ingest", "--source", "synthetic", "--days", "1"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["received"] == 0
        assert body["batch_id"].startswith("ing_")


class OrphanFeed:
    """A feed that names campaigns this database does not have.

    The signature of a mapping that quietly broke - an account renamed, a
    customer id rotated - and the reason the scheduler exits non-zero on "pulled
    rows, landed none" rather than only on an exception.
    """

    name = "orphan.feed"

    @property
    def is_configured(self) -> bool:
        return True

    async def fetch(
        self, *, start: date, end: date, targets: Sequence[SourceTarget] = ()
    ) -> list[SourceRecord]:
        """One unattributable row per day, so the counts stay arithmetic."""
        days = (end - start).days + 1
        return [
            SourceRecord(
                stat_date=start + timedelta(days=offset),
                platform=Platform.MOCK,
                external_id="ext_nobody_has_this",
                impressions=100,
                clicks=4,
                cost=10.0,
            )
            for offset in range(days)
        ]


@pytest.fixture
def orphan_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the whole feed registry for the orphan, leaving the CLI wiring intact."""

    def only_the_orphan(platforms: PlatformRegistry) -> MetricSourceRegistry:
        return MetricSourceRegistry([OrphanFeed()])

    monkeypatch.setattr("adoptimizer.core.container.build_metric_sources", only_the_orphan)


class TestScheduler:
    """The scheduled pull, driven the way a CronJob drives it.

    Counts come from the seeded portfolio: 8 campaigns and a 3-day nominal
    lookback, so a first pass touches 24 slots. The seed already stores 21 days
    of history, which means those 24 are updates rather than inserts - a fact
    worth asserting, because a scheduler that reported them as new rows would be
    double-counting.
    """

    LOOKBACK_DAYS = 3
    SEEDED_CAMPAIGNS = 8

    def seed(self) -> None:
        assert runner.invoke(app, ["seed"]).exit_code == 0

    def test_the_first_pass_pulls_the_nominal_lookback(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["ran"] == 1
        assert body["failed"] == 0
        tick = body["ticks"][0]
        assert tick["source"] == "synthetic"
        assert tick["outcome"] == "ran"
        assert tick["reason"] == "first"
        assert tick["received"] == self.SEEDED_CAMPAIGNS * self.LOOKBACK_DAYS
        assert tick["created"] == 0
        assert tick["updated"] == self.SEEDED_CAMPAIGNS * self.LOOKBACK_DAYS
        assert tick["batch_id"].startswith("ing_")

    def test_a_second_pass_finds_the_window_covered(self, cli_env: Path) -> None:
        """Re-running is cheap, which is what makes the schedule safe to retry."""
        self.seed()
        assert runner.invoke(app, ["scheduler", "--once"]).exit_code == 0

        second = runner.invoke(app, ["scheduler", "--once"])

        assert second.exit_code == 0, second.output
        body = payload(second.output)
        assert body["skipped"] == 1
        assert body["ran"] == 0
        assert body["ticks"][0]["outcome"] == "skipped"
        assert body["ticks"][0]["reason"] == "covered"
        assert body["ticks"][0]["batch_id"] is None

    def test_force_re_pulls_a_window_that_is_already_covered(self, cli_env: Path) -> None:
        self.seed()
        assert runner.invoke(app, ["scheduler", "--once"]).exit_code == 0

        forced = runner.invoke(app, ["scheduler", "--once", "--force"])

        assert forced.exit_code == 0, forced.output
        body = payload(forced.output)
        assert body["ran"] == 1
        # Forcing re-asserts stored numbers; it does not invent new slots.
        assert body["ticks"][0]["created"] == 0
        assert body["ticks"][0]["updated"] == self.SEEDED_CAMPAIGNS * self.LOOKBACK_DAYS

    def test_a_dry_run_leaves_the_next_pass_with_the_same_window(self, cli_env: Path) -> None:
        """A rehearsal that advanced the watermark would make the real run skip."""
        self.seed()

        rehearsal = runner.invoke(app, ["scheduler", "--once", "--dry-run"])
        assert rehearsal.exit_code == 0, rehearsal.output
        assert payload(rehearsal.output)["ticks"][0]["outcome"] == "ran"

        real = runner.invoke(app, ["scheduler", "--once"])

        assert real.exit_code == 0, real.output
        assert payload(real.output)["ticks"][0]["reason"] == "first"

    def test_the_source_flag_overrides_the_configured_feeds(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--source", "synthetic"])

        assert result.exit_code == 0, result.output
        ticks = payload(result.output)["ticks"]
        assert [tick["source"] for tick in ticks] == ["synthetic"]

    def test_an_unknown_feed_is_a_failed_tick_and_not_a_crash(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--source", "nope"])

        assert result.exit_code == 1, result.output
        tick = payload(result.output)["ticks"][0]
        assert tick["outcome"] == "failed"
        assert "Unknown metric source" in (tick["error"] or "")

    def test_the_platform_feed_fails_loudly_in_mock_mode(self, cli_env: Path) -> None:
        """A feed that cannot run must say so rather than report an empty success."""
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--source", "platform"])

        assert result.exit_code == 1, result.output
        body = payload(result.output)
        assert body["failed"] == 1
        assert body["ticks"][0]["outcome"] == "failed"
        assert body["ticks"][0]["error"]

    def test_a_feed_that_lands_nothing_exits_non_zero(
        self, cli_env: Path, orphan_feed: None
    ) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--source", "orphan.feed"])

        assert result.exit_code == 1, result.output
        tick = payload(result.output)["ticks"][0]
        assert tick["outcome"] == "ran"
        assert tick["received"] == self.LOOKBACK_DAYS
        assert tick["created"] == 0
        assert tick["updated"] == 0
        assert tick["unresolved_count"] == self.LOOKBACK_DAYS

    def test_the_disabled_loop_pulls_nothing_on_its_own(self, cli_env: Path) -> None:
        """Without --once the flag is a real off switch, not a warning to ignore."""
        self.seed()
        os.environ["INGEST__SCHEDULER_ENABLED"] = "false"

        try:
            get_settings.cache_clear()
            result = runner.invoke(app, ["scheduler"])

            assert result.exit_code == 0, result.output
            assert "resident loop is off" in payload(result.output)["warning"]

            # Nothing was pulled, so the next real pass still sees a first window.
            os.environ["INGEST__SCHEDULER_ENABLED"] = "true"
            get_settings.cache_clear()
            after = runner.invoke(app, ["scheduler", "--once"])
            assert payload(after.output)["ticks"][0]["reason"] == "first"
        finally:
            os.environ.pop("INGEST__SCHEDULER_ENABLED", None)
            get_settings.cache_clear()

    def test_once_is_not_gated_by_the_loop_switch(self, cli_env: Path) -> None:
        """This is what lets Kubernetes own the cadence in production."""
        self.seed()
        os.environ["INGEST__SCHEDULER_ENABLED"] = "false"

        try:
            get_settings.cache_clear()
            result = runner.invoke(app, ["scheduler", "--once"])

            assert result.exit_code == 0, result.output
            body = payload(result.output)
            assert "warning" not in body
            assert body["ran"] == 1
        finally:
            os.environ.pop("INGEST__SCHEDULER_ENABLED", None)
            get_settings.cache_clear()

    def test_an_out_of_range_cadence_is_refused_before_anything_is_pulled(
        self, cli_env: Path
    ) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--interval", "99999"])

        assert result.exit_code != 0

    def test_a_feed_name_that_cannot_be_a_label_is_refused(self, cli_env: Path) -> None:
        """--source becomes a Prometheus label, so it obeys the same shape rule."""
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--source", "Bad Name"])

        assert result.exit_code != 0

    def test_a_zero_interval_keeps_the_configured_cadence(self, cli_env: Path) -> None:
        self.seed()

        result = runner.invoke(app, ["scheduler", "--once", "--interval", "0"])

        assert result.exit_code == 0, result.output
        assert payload(result.output)["ran"] == 1


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


CREDENTIAL_ENV_KEYS = (
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
    "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID",
    "META_APP_SECRET",
    "TIKTOK_ACCESS_TOKEN",
    "TIKTOK_ADVERTISER_ID",
)


@pytest.fixture
def clean_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start and finish with no platform credentials in the environment.

    `load_dotenv` writes straight into `os.environ` and bypasses monkeypatch, so
    teardown has to remove the keys explicitly or one test's fake token leaks
    into the next.
    """
    for key in CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in CREDENTIAL_ENV_KEYS:
        os.environ.pop(key, None)


class TestMasking:
    """The whole point of `creds` is that its output is safe to share."""

    def test_an_absent_value_reports_only_its_absence(self) -> None:
        assert _mask("") == {"present": False}

    def test_a_present_value_reports_length_and_fingerprint_only(self) -> None:
        masked = _mask("super-secret-token-value")

        assert masked["present"] is True
        assert masked["length"] == len("super-secret-token-value")
        assert len(masked["sha256_8"]) == 8
        assert masked["warnings"] == []
        assert "super-secret-token-value" not in json.dumps(masked)

    def test_the_fingerprint_is_stable_and_value_specific(self) -> None:
        assert _mask("abc")["sha256_8"] == _mask("abc")["sha256_8"]
        assert _mask("abc")["sha256_8"] != _mask("abd")["sha256_8"]

    def test_surrounding_whitespace_is_called_out(self) -> None:
        assert any("whitespace" in item for item in _mask("  padded-token  ")["warnings"])

    def test_quoting_is_called_out(self) -> None:
        assert any("quoted" in item for item in _mask('"token"')["warnings"])
        assert any("quoted" in item for item in _mask("'token'")["warnings"])

    def test_an_unfilled_placeholder_is_called_out(self) -> None:
        assert any("placeholder" in item for item in _mask("<your-client-id>")["warnings"])
        assert any("placeholder" in item for item in _mask("CHANGEME")["warnings"])

    def test_a_plausible_value_draws_no_warnings(self) -> None:
        assert _mask("1//0gAbCdEfGh-real-looking-token")["warnings"] == []


class TestDotenvLoading:
    """A credential in `.env` must reach the adapters, which read os.getenv."""

    def test_the_dotenv_is_found_from_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text("DATA_MODE=warehouse\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert dotenv_path() == tmp_path / ".env"

    def test_load_environment_exports_dotenv_into_os_environ(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_credentials: None,
    ) -> None:
        (tmp_path / ".env").write_text(
            "GOOGLE_ADS_DEVELOPER_TOKEN=token-from-the-dotenv-file\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        loaded = load_environment()

        assert loaded == tmp_path / ".env"
        assert os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") == "token-from-the-dotenv-file"

    def test_a_real_environment_variable_beats_the_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_credentials: None,
    ) -> None:
        """`override=False` is what keeps a container deployment authoritative."""
        (tmp_path / ".env").write_text("META_AD_ACCOUNT_ID=from-file\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("META_AD_ACCOUNT_ID", "from-environment")

        load_environment()

        assert os.getenv("META_AD_ACCOUNT_ID") == "from-environment"

    def test_a_dotenv_credential_makes_the_adapter_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_credentials: None,
    ) -> None:
        """The regression this whole command exists to catch."""
        (tmp_path / ".env").write_text(
            "GOOGLE_ADS_CLIENT_ID=fake-client-id\n"
            "GOOGLE_ADS_CLIENT_SECRET=fake-client-secret\n"
            "GOOGLE_ADS_REFRESH_TOKEN=fake-refresh-token\n"
            "GOOGLE_ADS_DEVELOPER_TOKEN=fake-developer-token\n"
            "GOOGLE_ADS_CUSTOMER_ID=123-456-7890\n"
            "META_ACCESS_TOKEN=fake-meta-token\n"
            "META_AD_ACCOUNT_ID=act_1234567890\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        load_environment()

        status = build_platform_clients(DataMode.WAREHOUSE).status()

        assert status["google"]["configured"] is True
        assert status["meta"]["configured"] is True
        assert status["tiktok"]["configured"] is False

    def test_a_missing_file_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nowhere = tmp_path / "nowhere"
        empty = tmp_path / "nothing-here"
        empty.mkdir()
        monkeypatch.chdir(empty)

        assert dotenv_path(project_root=nowhere) is None
        assert load_environment(project_root=nowhere) is None

    def test_the_packaged_backend_env_is_the_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Launch from anywhere and `backend/.env` is still found."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / ".env").write_text("DATA_MODE=mock\n", encoding="utf-8")
        monkeypatch.chdir(elsewhere)

        assert dotenv_path(project_root=tmp_path) == tmp_path / ".env"


class TestCredsCommand:
    """`adoptimizer creds` is how an operator hands secrets over without showing them."""

    def test_missing_credentials_are_reported_per_platform(
        self, cli_env: Path, clean_credentials: None
    ) -> None:
        result = runner.invoke(app, ["creds"])

        assert result.exit_code == 0, result.output
        body = payload(result.output)
        assert body["platforms"]["google"]["ready"] is False
        assert "GOOGLE_ADS_DEVELOPER_TOKEN" in body["platforms"]["google"]["missing"]
        assert body["platforms"]["meta"]["ready"] is False
        assert body["platforms"]["tiktok"]["ready"] is False

    def test_a_filled_platform_is_reported_ready(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, clean_credentials: None
    ) -> None:
        monkeypatch.setenv("META_ACCESS_TOKEN", "fake-meta-token")
        monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_1234567890")

        body = payload(runner.invoke(app, ["creds"]).output)

        assert body["platforms"]["meta"]["ready"] is True
        assert body["platforms"]["meta"]["missing"] == []
        assert body["platforms"]["google"]["ready"] is False

    def test_no_secret_value_reaches_the_output(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, clean_credentials: None
    ) -> None:
        secret = "fake-meta-token-do-not-print"
        monkeypatch.setenv("META_ACCESS_TOKEN", secret)

        output = runner.invoke(app, ["creds"]).output

        assert secret not in output
        reported = payload(output)["platforms"]["meta"]["required"]["META_ACCESS_TOKEN"]
        assert reported["length"] == len(secret)
        assert reported["present"] is True

    def test_mock_data_mode_is_called_out(self, cli_env: Path, clean_credentials: None) -> None:
        body = payload(runner.invoke(app, ["creds"]).output)

        assert body["data_mode"] == "mock"
        assert any("DATA_MODE=mock" in note for note in body["notes"])

    def test_the_guardrails_are_reported_alongside(
        self, cli_env: Path, clean_credentials: None
    ) -> None:
        body = payload(runner.invoke(app, ["creds"]).output)

        assert body["tool_guardrails"] == {
            "enabled": True,
            "dry_run": False,
            "allow_agent_writes": False,
            "require_action_approval": True,
        }

    def test_agent_writes_are_warned_about_when_enabled(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, clean_credentials: None
    ) -> None:
        monkeypatch.setenv("TOOLS__ALLOW_AGENT_WRITES", "true")
        get_settings.cache_clear()

        body = payload(runner.invoke(app, ["creds"]).output)

        assert body["tool_guardrails"]["allow_agent_writes"] is True
        assert any("ALLOW_AGENT_WRITES" in note for note in body["notes"])
        get_settings.cache_clear()

    def test_the_llm_key_is_masked_too(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM__API_KEY", "")
        # Pinned rather than deleted: load_dotenv exports a developer .env into
        # os.environ with override=False, so an unset LLM__API_BASE here would
        # still inherit whatever base URL the local .env declares and this test
        # would fail on that machine only.
        monkeypatch.setenv("LLM__API_BASE", "")
        get_settings.cache_clear()

        body = payload(runner.invoke(app, ["creds"]).output)

        assert body["llm"]["provider"] == "mock"
        assert body["llm"]["api_key"] == {"present": False}
        assert body["llm"]["api_base"] in (None, "")
        get_settings.cache_clear()

    def test_a_custom_endpoint_reports_its_base_url(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """api_base is not a secret, and forgetting it is the classic

        openai_compatible misconfiguration: the provider raises at call time and
        the gateway degrades to mock. So "creds" shows the base URL plainly,
        while the key beside it stays masked."""
        monkeypatch.setenv("LLM__PROVIDER", "openai_compatible")
        monkeypatch.setenv("LLM__API_KEY", "fake-compatible-key-do-not-print")
        monkeypatch.setenv("LLM__API_BASE", "https://api.deepseek.com/v1")
        get_settings.cache_clear()

        output = runner.invoke(app, ["creds"]).output

        assert "fake-compatible-key-do-not-print" not in output
        body = payload(output)
        assert body["llm"]["provider"] == "openai_compatible"
        assert body["llm"]["api_base"] == "https://api.deepseek.com/v1"
        assert body["llm"]["api_key"]["present"] is True
        assert body["llm"]["api_key"]["length"] == len("fake-compatible-key-do-not-print")
        get_settings.cache_clear()

    def test_probe_exits_non_zero_when_nothing_is_configured(
        self, cli_env: Path, clean_credentials: None
    ) -> None:
        """Unconfigured adapters are refused before any socket is opened."""
        result = runner.invoke(app, ["creds", "--probe", "--external-id", "ext_google_1000"])

        assert result.exit_code == 1, result.output
        body = payload(result.output)
        for platform in ("google", "meta", "tiktok"):
            entry = body["probe"]["platforms"][platform]
            assert entry["ok"] is False
            assert "not configured" in entry["error"]

    def test_probe_reports_the_external_id_it_used(
        self, cli_env: Path, clean_credentials: None
    ) -> None:
        body = payload(
            runner.invoke(app, ["creds", "--probe", "--external-id", "ext_probe_1"]).output
        )

        assert body["probe"]["external_id"] == "ext_probe_1"

    def test_probe_needs_an_external_id_when_the_database_is_empty(
        self, cli_env: Path, clean_credentials: None
    ) -> None:
        """An empty database must say so rather than probe with an empty id."""
        from adoptimizer.infra.db.session import init_database

        settings = get_settings()

        async def create_empty_schema() -> None:
            database = await init_database(settings.database)
            await database.create_all()
            await database.dispose()

        import asyncio

        asyncio.run(create_empty_schema())

        body = payload(runner.invoke(app, ["creds", "--probe"]).output)

        assert body["probe"]["external_id"] == ""
        assert "no campaign carries an external_id" in body["probe"]["error"]
        assert body["probe"]["platforms"] == {}
        _ = init_database
