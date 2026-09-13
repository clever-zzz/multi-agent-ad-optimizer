"""Integration tests for the tool layer as the API exposes it.

The unit tests pin the executor's guardrails; these pin what an operator can
actually see: the capability catalogue, the audit trail, the guardrail state on
the status pages, and the tool counters folded into every run summary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from adoptimizer.app import create_app
from adoptimizer.core.config import DatabaseSettings, DataMode, Settings, ToolSettings
from adoptimizer.domain.enums import AgentName, Platform
from adoptimizer.infra.ads.mock import MockAdsClient
from adoptimizer.infra.ads.registry import PlatformRegistry
from adoptimizer.infra.db.session import Database
from adoptimizer.tools import (
    DatabaseToolAudit,
    ToolExecutor,
    ToolRequest,
    build_tool_executor,
)

from ..conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    API,
    access_token,
    bearer,
    make_settings,
)

PLATFORM_TOOLS = {
    "platform.campaign_report",
    "platform.set_daily_budget",
    "platform.pause_campaign",
    "platform.resume_campaign",
    "platform.pause_creative",
    "platform.resume_creative",
    "platform.create_creative",
}


@asynccontextmanager
async def standalone(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Boot a second app instance so a guardrail can be flipped for one test."""
    app: FastAPI = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as http,
    ):
        yield http


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


async def run_sync(
    client: httpx.AsyncClient, headers: dict[str, str], **payload: Any
) -> dict[str, Any]:
    """Trigger a run and block until it finishes."""
    body: dict[str, Any] = {"background": False, "max_iterations": 1, "window_days": 7}
    body.update(payload)
    response = await client.post(API + "/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "succeeded", run.get("error_message")
    return run


class TestCatalogueEndpoint:
    async def test_admin_sees_every_platform_tool(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools", headers=admin)).json()
        assert {tool["name"] for tool in body["tools"]} == PLATFORM_TOOLS

    async def test_the_guardrails_match_the_running_configuration(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools", headers=admin)).json()
        assert body["guardrails"] == {
            "enabled": True,
            "dry_run": False,
            "allow_agent_writes": False,
            "max_calls_per_run": 500,
            "audit_persist": True,
            "require_action_approval": True,
            "data_mode": "mock",
        }

    async def test_the_catalogue_states_who_may_change_a_budget(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools", headers=admin)).json()
        by_name = {tool["name"]: tool for tool in body["tools"]}

        assert by_name["platform.campaign_report"]["read_only"] is True
        assert by_name["platform.campaign_report"]["agents"] == []
        assert by_name["platform.set_daily_budget"]["read_only"] is False
        assert by_name["platform.set_daily_budget"]["agents"] == ["optimize"]
        assert by_name["platform.create_creative"]["agents"] == ["creative"]
        assert body["write_tools"] == sorted(PLATFORM_TOOLS - {"platform.campaign_report"})

    async def test_a_monitoring_agent_can_only_read(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools", headers=admin)).json()
        assert body["by_agent"]["monitor"] == ["platform.campaign_report"]
        assert body["by_agent"]["critic"] == ["platform.campaign_report"]
        assert "platform.pause_campaign" in body["by_agent"]["optimize"]

    async def test_the_parameter_schema_travels_with_the_tool(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools", headers=admin)).json()
        by_name = {tool["name"]: tool for tool in body["tools"]}
        params = by_name["platform.set_daily_budget"]["parameters"]

        assert params["additionalProperties"] is False
        assert set(params["properties"]) == {
            "platform",
            "campaign_external_id",
            "daily_budget",
            "reason",
        }

    async def test_a_viewer_cannot_read_the_catalogue(
        self, client: httpx.AsyncClient, make_user: Any
    ) -> None:
        viewer = await make_user("viewer")
        response = await client.get(API + "/admin/tools", headers=viewer["headers"])
        assert response.status_code == 403


class TestAuditEndpoint:
    async def test_an_empty_window_reports_zeroes(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/tools/invocations", headers=admin)).json()
        assert body["totals"] == {"calls": 0, "dry_run": 0, "refused": 0, "failed": 0}
        assert body["by_tool"] == []
        assert body["window_days"] == 30

    async def test_a_run_filter_is_accepted(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(
            API + "/admin/tools/invocations",
            params={"run_id": "run_missing", "days": 7},
            headers=admin,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == "run_missing"
        assert body["invocations"] == []

    async def test_an_absurd_window_is_refused(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(
            API + "/admin/tools/invocations", params={"days": 9999}, headers=admin
        )
        assert response.status_code == 422

    async def test_a_viewer_cannot_read_the_audit_trail(
        self, client: httpx.AsyncClient, make_user: Any
    ) -> None:
        viewer = await make_user("viewer")
        response = await client.get(API + "/admin/tools/invocations", headers=viewer["headers"])
        assert response.status_code == 403


class TestRunSummaryCarriesTheToolLayer:
    async def test_a_completed_run_reports_its_tool_counters(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)
        tools = run["summary"]["tools"]

        assert tools["tools_registered"] == len(PLATFORM_TOOLS)
        assert tools["budget"] == 500
        assert tools["agent_writes_allowed"] is False
        assert tools["dry_run_by_default"] is False

    async def test_every_write_an_agent_makes_is_a_dry_run(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        """The invariant that replaced "no agent reaches a platform".

        Agents now read live reports and rehearse their own proposals, so the
        property worth pinning is narrower and stronger: the only invocations
        that reach a network are reads, and anything an agent asks for that
        could change spend comes back as a dry run with nothing applied.
        """
        run = await run_sync(client, admin)
        tools = run["summary"]["tools"]
        audit = (
            await client.get(
                API + "/admin/tools/invocations",
                params={"run_id": run["id"]},
                headers=admin,
            )
        ).json()
        invocations = audit["invocations"]

        assert tools["calls"] > 0
        assert len(invocations) == tools["invocations"]
        assert {row["agent"] for row in invocations} <= {"monitor", "optimize"}

        reads = [row for row in invocations if row["read_only"]]
        writes = [row for row in invocations if not row["read_only"]]
        assert reads, "the monitor should cross-check its worst alerts live"
        assert all(row["tool"] == "platform.campaign_report" for row in reads)
        assert all(row["outcome"] == "success" for row in reads)
        assert writes, "the optimizer should rehearse its own proposals"
        assert all(row["dry_run"] for row in writes)
        assert all(row["outcome"] == "dry_run" for row in writes)
        assert audit["totals"]["dry_run"] == len(writes)
        assert tools["writes"] == len(writes)


class TestStatusPages:
    async def test_readyz_reports_the_tool_layer(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/readyz")).json()
        tools = body["dependencies"]["tools"]

        assert tools["status"] == "ok"
        assert tools["enabled"] is True
        assert tools["registered"] == len(PLATFORM_TOOLS)
        assert tools["agent_writes_allowed"] is False

    async def test_system_info_reports_the_guardrails(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/admin/system", headers=admin)).json()

        assert body["tools_enabled"] is True
        assert body["tools_dry_run"] is False
        assert body["tools_allow_agent_writes"] is False

    async def test_the_public_info_endpoint_reports_them_too(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/system/info")).json()
        assert body["tools_enabled"] is True
        assert body["tools_allow_agent_writes"] is False


class TestGuardrailsCanBeFlipped:
    async def test_paper_trading_mode_is_reported(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "dryrun.db").as_posix(),
            tools=ToolSettings(dry_run=True),
        )
        async with standalone(settings) as http:
            token = await access_token(http, ADMIN_EMAIL, ADMIN_PASSWORD)
            body = (await http.get("/readyz")).json()["dependencies"]["tools"]
            catalogue = (await http.get(API + "/admin/tools", headers=bearer(token))).json()

        assert body["dry_run_by_default"] is True
        assert catalogue["guardrails"]["dry_run"] is True

    async def test_a_disabled_tool_layer_says_so(self, tmp_path: Path) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "disabled.db").as_posix(),
            tools=ToolSettings(enabled=False),
        )
        async with standalone(settings) as http:
            body = (await http.get("/readyz")).json()["dependencies"]["tools"]

        assert body["status"] == "disabled"
        assert body["enabled"] is False

    async def test_agent_writes_can_be_permitted_explicitly(self, tmp_path: Path) -> None:
        """The switch exists, is off by default, and is visible when turned on."""
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "writes.db").as_posix(),
            tools=ToolSettings(allow_agent_writes=True, max_calls_per_run=12),
        )
        async with standalone(settings) as http:
            token = await access_token(http, ADMIN_EMAIL, ADMIN_PASSWORD)
            catalogue = (await http.get(API + "/admin/tools", headers=bearer(token))).json()

        assert catalogue["guardrails"]["allow_agent_writes"] is True
        assert catalogue["guardrails"]["max_calls_per_run"] == 12


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """A throwaway in-memory store, the same shape the app boots with."""
    instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await instance.create_all()
    yield instance
    await instance.dispose()


REPORT_ARGS = {
    "platform": "google",
    "campaign_external_id": "ext_1",
    "start_date": "2026-09-01",
    "end_date": "2026-09-07",
}

BUDGET_ARGS = {
    "platform": "meta",
    "campaign_external_id": "ext_1",
    "daily_budget": 250.0,
    "reason": "burn rate is 3x the daily budget",
}


def audited_executor(database: Database, **overrides: Any) -> tuple[ToolExecutor, MockAdsClient]:
    """An executor whose audit sink is the real database-backed one."""
    client = MockAdsClient(Platform.MOCK)
    platforms = PlatformRegistry({Platform.MOCK: client}, data_mode=DataMode.MOCK)
    executor = build_tool_executor(
        platforms,
        ToolSettings(**overrides),
        sink=DatabaseToolAudit(database.session_factory),
    )
    return executor, client


def read_request(run_id: str = "run_1") -> ToolRequest:
    return ToolRequest(
        tool="platform.campaign_report",
        arguments=REPORT_ARGS,
        agent=AgentName.MONITOR,
        run_id=run_id,
    )


def budget_request(agent: AgentName | None, run_id: str = "run_1") -> ToolRequest:
    return ToolRequest(
        tool="platform.set_daily_budget",
        arguments=BUDGET_ARGS,
        agent=agent,
        actor="system" if agent is not None else "operator:u1",
        run_id=run_id,
    )


class TestDatabaseToolAudit:
    async def test_a_recorded_invocation_can_be_read_back(self, database: Database) -> None:
        executor, _ = audited_executor(database)
        await executor.call(read_request())

        rows = await DatabaseToolAudit(database.session_factory).for_run("run_1")

        assert len(rows) == 1
        row = rows[0]
        assert row["id"].startswith("tool_")
        assert row["tool"] == "platform.campaign_report"
        assert row["agent"] == "monitor"
        assert row["actor"] == "system"
        assert row["outcome"] == "success"
        assert row["read_only"] is True
        assert row["dry_run"] is False
        assert row["touches_platform"] is True
        assert row["arguments"] == REPORT_ARGS
        assert row["created_at"]

    async def test_a_preflighted_write_is_marked_as_a_dry_run(self, database: Database) -> None:
        executor, client = audited_executor(database)
        await executor.call(budget_request(AgentName.OPTIMIZE))

        row = (await DatabaseToolAudit(database.session_factory).for_run("run_1"))[0]

        assert row["outcome"] == "dry_run"
        assert row["dry_run"] is True
        assert row["read_only"] is False
        assert "TOOLS__ALLOW_AGENT_WRITES" in row["result"]["reason"]
        assert client.calls == []

    async def test_an_executed_write_reaches_the_adapter_and_is_recorded(
        self, database: Database
    ) -> None:
        executor, client = audited_executor(database)
        await executor.call(budget_request(None))

        row = (await DatabaseToolAudit(database.session_factory).for_run("run_1"))[0]

        assert row["outcome"] == "success"
        assert row["dry_run"] is False
        assert row["agent"] == ""
        assert row["actor"] == "operator:u1"
        assert row["result"]["success"] is True
        assert len(client.calls) == 1

    async def test_a_refusal_is_audited_with_its_reason(self, database: Database) -> None:
        """The refusals are the rows an operator actually reads."""
        executor, client = audited_executor(database)
        await executor.call(budget_request(AgentName.CREATIVE))

        row = (await DatabaseToolAudit(database.session_factory).for_run("run_1"))[0]

        assert row["outcome"] == "permission_denied"
        assert row["error"] is not None and "creative" in row["error"]
        assert row["result"] == {}
        assert client.calls == []

    async def test_a_handler_failure_is_recorded_as_failed(self, database: Database) -> None:
        platforms = PlatformRegistry(
            {Platform.MOCK: MockAdsClient(Platform.MOCK)}, data_mode=DataMode.WAREHOUSE
        )
        executor = build_tool_executor(
            platforms, ToolSettings(), sink=DatabaseToolAudit(database.session_factory)
        )
        await executor.call(read_request())

        row = (await DatabaseToolAudit(database.session_factory).for_run("run_1"))[0]

        assert row["outcome"] == "failed"
        assert row["error"] is not None and "No adapter registered" in row["error"]

    async def test_the_summary_separates_outcomes(self, database: Database) -> None:
        executor, _ = audited_executor(database)
        await executor.call(read_request())
        await executor.call(budget_request(AgentName.OPTIMIZE))
        await executor.call(budget_request(AgentName.CREATIVE))

        summary = await DatabaseToolAudit(database.session_factory).summary(days=30)

        assert summary["totals"] == {"calls": 3, "dry_run": 1, "refused": 1, "failed": 0}
        by_tool = {entry["tool"]: entry for entry in summary["by_tool"]}
        assert by_tool["platform.campaign_report"]["calls"] == 1
        assert by_tool["platform.set_daily_budget"]["calls"] == 2
        assert by_tool["platform.set_daily_budget"]["dry_run"] == 1

    async def test_the_summary_can_be_scoped_to_one_run(self, database: Database) -> None:
        executor, _ = audited_executor(database)
        await executor.call(read_request("run_1"))
        await executor.call(read_request("run_2"))

        audit = DatabaseToolAudit(database.session_factory)

        assert (await audit.summary(run_id="run_1"))["totals"]["calls"] == 1
        assert len(await audit.for_run("run_2")) == 1

    async def test_calls_without_a_run_are_still_audited(self, database: Database) -> None:
        executor, _ = audited_executor(database)
        await executor.call(read_request(run_id=""))

        summary = await DatabaseToolAudit(database.session_factory).summary(days=30)

        assert summary["totals"]["calls"] == 1
        assert summary["run_id"] is None

    async def test_an_unreachable_store_does_not_break_the_call(self) -> None:
        """Losing an audit line is bad; taking the optimizer down over it is worse."""
        instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
        await instance.create_all()
        executor, _ = audited_executor(instance)
        await instance.dispose()

        result = await executor.call(read_request())

        assert result.outcome.value == "success"

    async def test_audit_persistence_can_be_turned_off(self, tmp_path: Path) -> None:
        """`TOOLS__AUDIT_PERSIST=false` swaps in the no-op sink; the app still boots."""
        settings = make_settings(
            "sqlite+aiosqlite:///" + (tmp_path / "noaudit.db").as_posix(),
            tools=ToolSettings(audit_persist=False),
        )
        async with standalone(settings) as http:
            ready = (await http.get("/readyz")).json()["dependencies"]["tools"]
            token = await access_token(http, ADMIN_EMAIL, ADMIN_PASSWORD)
            audit = (await http.get(API + "/admin/tools/invocations", headers=bearer(token))).json()

        assert ready["registered"] == len(PLATFORM_TOOLS)
        assert audit["totals"]["calls"] == 0
