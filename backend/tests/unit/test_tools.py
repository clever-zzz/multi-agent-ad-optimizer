"""Tool layer: capability catalogue, executor guardrails, platform binding.

These tests are the specification for the one property that matters most: an
agent can read whatever it is granted, but it cannot make a live ad platform do
anything. Every path that could reach a mutation is asserted against the mock
adapter's own call log, so a regression here fails loudly rather than quietly
spending someone's budget.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import Field

from adoptimizer.core.config import DataMode, ToolSettings
from adoptimizer.domain.enums import AgentName, Platform
from adoptimizer.infra.ads.mock import MockAdsClient
from adoptimizer.infra.ads.registry import PlatformRegistry
from adoptimizer.tools import (
    ToolExecutor,
    ToolRegistry,
    ToolRequest,
    ToolResult,
    build_platform_tools,
    build_tool_executor,
)
from adoptimizer.tools.spec import ToolArguments, ToolOutcome, ToolSpec

# The catalogue the platform binding is expected to produce, asserted as a set so
# adding a tool without updating the tests is a visible failure.
PLATFORM_TOOL_NAMES = {
    "platform.campaign_report",
    "platform.set_daily_budget",
    "platform.pause_campaign",
    "platform.resume_campaign",
    "platform.pause_creative",
    "platform.resume_creative",
    "platform.create_creative",
}


class RecordingBus:
    """Stands in for EventBus and keeps everything published to it."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, run_id: str, event_type: str, **kwargs: Any) -> None:
        self.published.append({"run_id": run_id, "event_type": event_type, **kwargs})


class RecordingSink:
    """Stands in for the audit sink."""

    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[ToolResult] = []
        self._fail = fail

    async def record(self, result: ToolResult) -> None:
        if self._fail:
            msg = "audit store is down"
            raise RuntimeError(msg)
        self.records.append(result)


class EchoArgs(ToolArguments):
    value: str = Field(min_length=1, max_length=20)


class MutateArgs(ToolArguments):
    target: str = Field(min_length=1)
    amount: float = Field(gt=0)


class Spy:
    """Records whether a handler actually ran."""

    def __init__(self) -> None:
        self.calls: list[ToolArguments] = []

    async def read(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        self.calls.append(args)
        _ = request
        return {"echo": getattr(args, "value", "")}

    async def write(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        self.calls.append(args)
        return {"mutated": getattr(args, "target", ""), "by": request.agent}

    async def explode(self, args: ToolArguments, request: ToolRequest) -> dict[str, Any]:
        _ = args, request
        msg = "the adapter is on fire"
        raise RuntimeError(msg)


@pytest.fixture
def spy() -> Spy:
    return Spy()


@pytest.fixture
def registry(spy: Spy) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="demo.echo",
                description="Return the value it was given.",
                parameters=EchoArgs,
                handler=spy.read,
                read_only=True,
            ),
            ToolSpec(
                name="demo.mutate",
                description="Change something that costs money.",
                parameters=MutateArgs,
                handler=spy.write,
                read_only=False,
                touches_platform=True,
                agents=frozenset({AgentName.OPTIMIZE}),
            ),
            ToolSpec(
                name="demo.explode",
                description="Always fails, for error-path tests.",
                parameters=EchoArgs,
                handler=spy.explode,
                read_only=True,
            ),
        ]
    )


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


def make_executor(
    registry: ToolRegistry,
    bus: RecordingBus,
    sink: Any,
    **overrides: Any,
) -> ToolExecutor:
    return ToolExecutor(registry, settings=ToolSettings(**overrides), bus=bus, sink=sink)


def read_request(agent: AgentName | None = AgentName.MONITOR, **kwargs: Any) -> ToolRequest:
    return ToolRequest(
        tool=kwargs.pop("tool", "demo.echo"),
        arguments=kwargs.pop("arguments", {"value": "hi"}),
        agent=agent,
        **kwargs,
    )


def write_request(agent: AgentName | None = AgentName.OPTIMIZE, **kwargs: Any) -> ToolRequest:
    return ToolRequest(
        tool=kwargs.pop("tool", "demo.mutate"),
        arguments=kwargs.pop("arguments", {"target": "camp_a", "amount": 10.0}),
        agent=agent,
        **kwargs,
    )


class TestToolSpec:
    def test_an_empty_allow_list_means_every_agent(self, registry: ToolRegistry) -> None:
        spec = registry.get("demo.echo")
        assert spec is not None
        assert all(spec.allows(agent) for agent in AgentName)

    def test_a_filled_allow_list_is_a_whitelist(self, registry: ToolRegistry) -> None:
        spec = registry.get("demo.mutate")
        assert spec is not None
        assert spec.allows(AgentName.OPTIMIZE)
        assert not spec.allows(AgentName.CREATIVE)

    def test_a_human_caller_is_never_filtered_by_the_agent_list(
        self, registry: ToolRegistry
    ) -> None:
        """`agent=None` means an approved execution, not an agent."""
        spec = registry.get("demo.mutate")
        assert spec is not None
        assert spec.allows(None)

    def test_the_json_schema_is_generated_from_the_model(self, registry: ToolRegistry) -> None:
        spec = registry.get("demo.mutate")
        assert spec is not None
        schema = spec.json_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "demo.mutate"
        params = schema["function"]["parameters"]
        assert set(params["properties"]) == {"target", "amount"}
        assert params["additionalProperties"] is False
        assert set(params["required"]) == {"target", "amount"}

    def test_describe_exposes_the_guardrail_relevant_flags(self, registry: ToolRegistry) -> None:
        spec = registry.get("demo.mutate")
        assert spec is not None
        described = spec.describe()
        assert described["read_only"] is False
        assert described["touches_platform"] is True
        assert described["agents"] == ["optimize"]


class TestToolRegistry:
    def test_registering_the_same_name_twice_is_refused(self, registry: ToolRegistry) -> None:
        spec = registry.get("demo.echo")
        assert spec is not None
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)

    def test_names_are_sorted_and_complete(self, registry: ToolRegistry) -> None:
        assert registry.names() == ["demo.echo", "demo.explode", "demo.mutate"]
        assert len(registry) == 3

    def test_an_unknown_tool_is_not_an_error_to_look_up(self, registry: ToolRegistry) -> None:
        assert registry.get("demo.nope") is None

    def test_for_agent_filters_by_capability(self) -> None:
        catalogue = ToolRegistry(build_platform_tools(_mock_registry()))
        creative = {spec.name for spec in catalogue.for_agent(AgentName.CREATIVE)}
        optimize = {spec.name for spec in catalogue.for_agent(AgentName.OPTIMIZE)}
        monitor = {spec.name for spec in catalogue.for_agent(AgentName.MONITOR)}

        assert "platform.create_creative" in creative
        assert "platform.set_daily_budget" not in creative
        assert "platform.set_daily_budget" in optimize
        assert monitor == {"platform.campaign_report"}

    def test_schemas_for_agent_are_function_calling_shaped(self) -> None:
        catalogue = ToolRegistry(build_platform_tools(_mock_registry()))
        schemas = catalogue.schemas_for_agent(AgentName.OPTIMIZE)
        assert schemas
        assert all(entry["type"] == "function" for entry in schemas)
        assert all("parameters" in entry["function"] for entry in schemas)

    def test_describe_lists_every_agent_and_the_write_tools(self) -> None:
        catalogue = ToolRegistry(build_platform_tools(_mock_registry()))
        described = catalogue.describe()
        assert set(described["by_agent"]) == {agent.value for agent in AgentName}
        assert "platform.campaign_report" not in described["write_tools"]
        assert "platform.pause_campaign" in described["write_tools"]


def _mock_registry() -> PlatformRegistry:
    return PlatformRegistry({Platform.MOCK: MockAdsClient(Platform.MOCK)}, data_mode=DataMode.MOCK)


class TestExecutorRefusals:
    async def test_an_unknown_tool_is_refused_with_a_reason(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(read_request(tool="demo.nope"))

        assert result.outcome is ToolOutcome.VALIDATION_FAILED
        assert result.error is not None and "unknown tool" in result.error
        assert not result.ok

    async def test_a_disabled_tool_layer_refuses_everything(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink, enabled=False)
        result = await executor.call(read_request())

        assert result.outcome is ToolOutcome.DISABLED
        assert executor.enabled is False

    async def test_an_agent_outside_the_allow_list_is_refused(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink, allow_agent_writes=True)
        result = await executor.call(write_request(agent=AgentName.CREATIVE))

        assert result.outcome is ToolOutcome.PERMISSION_DENIED
        assert result.error is not None and "creative" in result.error
        assert spy.calls == []

    async def test_bad_arguments_are_refused_before_the_handler_runs(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(read_request(arguments={"value": ""}))

        assert result.outcome is ToolOutcome.VALIDATION_FAILED
        assert result.error is not None and "invalid arguments" in result.error
        assert spy.calls == []

    async def test_an_invented_argument_is_refused_not_ignored(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        """`extra="forbid"` is what stops a hallucinated field passing silently."""
        executor = make_executor(registry, bus, sink)
        result = await executor.call(read_request(arguments={"value": "hi", "spend": 9999}))

        assert result.outcome is ToolOutcome.VALIDATION_FAILED
        assert spy.calls == []

    async def test_the_run_budget_is_enforced(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink, max_calls_per_run=2)
        first = await executor.call(read_request(run_id="run_1"))
        second = await executor.call(read_request(run_id="run_1"))
        third = await executor.call(read_request(run_id="run_1"))

        assert (first.outcome, second.outcome) == (ToolOutcome.SUCCESS, ToolOutcome.SUCCESS)
        assert third.outcome is ToolOutcome.BUDGET_EXHAUSTED
        assert third.error is not None and "tool budget" in third.error

    async def test_the_budget_is_per_run(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink, max_calls_per_run=1)
        await executor.call(read_request(run_id="run_1"))
        other = await executor.call(read_request(run_id="run_2"))

        assert other.outcome is ToolOutcome.SUCCESS

    async def test_a_refused_call_still_consumes_budget(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        """An agent spamming invalid calls must not get an unlimited retry loop."""
        executor = make_executor(registry, bus, sink, max_calls_per_run=1)
        await executor.call(read_request(run_id="run_1", arguments={"value": ""}))
        retry = await executor.call(read_request(run_id="run_1"))

        assert retry.outcome is ToolOutcome.BUDGET_EXHAUSTED

    async def test_a_human_execution_is_not_charged_to_the_run_budget(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        """The cap bounds what a model may do alone, not what an operator approved.

        A busy run spends its budget on preflights. If an approved execution were
        charged to the same counter, the guardrail could starve the one call that
        already cleared a stronger gate and become the cause of the outage it
        exists to prevent. It is still audited against the run.
        """
        executor = make_executor(registry, bus, sink, max_calls_per_run=1)

        agent_call = await executor.call(read_request(run_id="run_1"))
        exhausted = await executor.call(read_request(run_id="run_1"))
        human_call = await executor.call(
            write_request(agent=None, run_id="run_1", actor="usr_operator")
        )
        blocked_again = await executor.call(read_request(run_id="run_1"))

        assert agent_call.outcome is ToolOutcome.SUCCESS
        assert exhausted.outcome is ToolOutcome.BUDGET_EXHAUSTED
        assert human_call.outcome is ToolOutcome.SUCCESS
        assert human_call.dry_run is False
        assert blocked_again.outcome is ToolOutcome.BUDGET_EXHAUSTED

        usage = executor.usage("run_1")
        assert usage["calls"] == 1
        assert usage["invocations"] == 4
        assert usage["by_tool"] == {"demo.echo": 3, "demo.mutate": 1}
        assert usage["refusals"] == 2


class TestDryRunInterlock:
    async def test_an_agent_write_is_preflighted_not_executed(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(write_request())

        assert result.outcome is ToolOutcome.DRY_RUN
        assert result.ok is True
        assert result.dry_run is True
        assert spy.calls == []
        assert result.data["would_call"] == "demo.mutate"

    async def test_the_preflight_names_the_guardrail_that_fired(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(write_request())

        assert "TOOLS__ALLOW_AGENT_WRITES" in result.data["reason"]

    async def test_a_human_caller_may_execute_a_write(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(write_request(agent=None, actor="operator:u1"))

        assert result.outcome is ToolOutcome.SUCCESS
        assert len(spy.calls) == 1

    async def test_allow_agent_writes_lifts_the_interlock(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink, allow_agent_writes=True)
        result = await executor.call(write_request())

        assert result.outcome is ToolOutcome.SUCCESS
        assert len(spy.calls) == 1

    async def test_paper_trading_mode_stops_even_an_approved_write(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink, dry_run=True)
        result = await executor.call(write_request(agent=None))

        assert result.outcome is ToolOutcome.DRY_RUN
        assert "paper-trading" in result.data["reason"]
        assert spy.calls == []

    async def test_a_caller_may_ask_for_a_dry_run_explicitly(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink, allow_agent_writes=True)
        result = await executor.call(write_request(dry_run=True))

        assert result.outcome is ToolOutcome.DRY_RUN
        assert spy.calls == []

    async def test_a_read_tool_is_never_dry_run(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink, dry_run=True)
        result = await executor.call(read_request())

        assert result.outcome is ToolOutcome.SUCCESS
        assert result.data == {"echo": "hi"}
        assert len(spy.calls) == 1


class TestIdempotency:
    async def test_the_same_key_replays_the_first_result(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        first = await executor.call(read_request(run_id="run_1", idempotency_key="k1"))
        second = await executor.call(read_request(run_id="run_1", idempotency_key="k1"))

        assert first.outcome is ToolOutcome.SUCCESS
        assert second.outcome is ToolOutcome.REPLAYED
        assert second.data == first.data
        assert len(spy.calls) == 1

    async def test_a_request_without_a_key_is_never_replayed(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(read_request(run_id="run_1"))
        await executor.call(read_request(run_id="run_1"))

        assert len(spy.calls) == 2

    async def test_keys_are_scoped_to_their_run(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink, spy: Spy
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(read_request(run_id="run_1", idempotency_key="k1"))
        other = await executor.call(read_request(run_id="run_2", idempotency_key="k1"))

        assert other.outcome is ToolOutcome.SUCCESS
        assert len(spy.calls) == 2


class TestObservability:
    async def test_a_successful_call_is_published_on_the_bus(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(read_request(run_id="run_1"))

        assert len(bus.published) == 1
        event = bus.published[0]
        assert event["event_type"] == "tool.invoked"
        assert event["payload"]["tool"] == "demo.echo"
        assert event["payload"]["outcome"] == "success"

    async def test_refusals_are_published_too(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(write_request(run_id="run_1", agent=AgentName.CREATIVE))

        assert bus.published[0]["payload"]["outcome"] == "permission_denied"

    async def test_nothing_is_published_without_a_run(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(read_request())

        assert bus.published == []

    async def test_the_sink_receives_every_outcome(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        await executor.call(read_request(run_id="run_1"))
        await executor.call(read_request(run_id="run_1", tool="demo.nope"))

        assert [record.outcome for record in sink.records] == [
            ToolOutcome.SUCCESS,
            ToolOutcome.VALIDATION_FAILED,
        ]

    async def test_a_failing_audit_store_does_not_break_the_call(
        self, registry: ToolRegistry, bus: RecordingBus
    ) -> None:
        executor = make_executor(registry, bus, RecordingSink(fail=True))
        result = await executor.call(read_request(run_id="run_1"))

        assert result.outcome is ToolOutcome.SUCCESS

    async def test_a_handler_exception_becomes_a_failed_result(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(read_request(tool="demo.explode", run_id="run_1"))

        assert result.outcome is ToolOutcome.FAILED
        assert result.ok is False
        assert result.error is not None and "on fire" in result.error

    async def test_durations_are_reported_in_milliseconds(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        result = await executor.call(read_request(run_id="run_1"))

        assert result.duration_ms >= 0
        assert bus.published[0]["payload"]["duration_ms"] >= 0

    async def test_usage_reports_the_guardrails_alongside_the_counters(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink, max_calls_per_run=7)
        await executor.call(read_request(run_id="run_1"))

        assert executor.usage("run_1") == {
            "calls": 1,
            "invocations": 1,
            "budget": 7,
            "by_outcome": {"success": 1},
            "by_tool": {"demo.echo": 1},
            "writes": 0,
            "dry_runs": 0,
            "refusals": 0,
            "failures": 0,
            "dry_run_by_default": False,
            "agent_writes_allowed": False,
            "tools_registered": 3,
        }

    async def test_a_run_that_made_no_calls_reports_zero(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        assert executor.usage("run_never_seen")["calls"] == 0

    async def test_the_result_serialises_for_the_run_summary(
        self, registry: ToolRegistry, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        executor = make_executor(registry, bus, sink)
        payload = (await executor.call(read_request(run_id="run_1"))).to_dict()

        assert payload["tool"] == "demo.echo"
        assert payload["agent"] == "monitor"
        assert payload["outcome"] == "success"
        assert payload["dry_run"] is False


class TestPlatformTools:
    @pytest.fixture
    def client(self) -> MockAdsClient:
        return MockAdsClient(Platform.MOCK)

    @pytest.fixture
    def executor(
        self, client: MockAdsClient, bus: RecordingBus, sink: RecordingSink
    ) -> ToolExecutor:
        platforms = PlatformRegistry({Platform.MOCK: client}, data_mode=DataMode.MOCK)
        return make_executor(ToolRegistry(build_platform_tools(platforms)), bus, sink)

    async def test_a_report_read_reaches_the_adapter(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.campaign_report",
                arguments={
                    "platform": "google",
                    "campaign_external_id": "ext_1",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-07",
                },
                agent=AgentName.MONITOR,
                run_id="run_1",
            )
        )

        assert result.outcome is ToolOutcome.SUCCESS
        assert result.data["campaign_id"] == "ext_1"
        assert client.calls == []

    async def test_the_result_names_the_adapter_that_served_it(
        self, executor: ToolExecutor
    ) -> None:
        """Mock mode routes every platform to the mock; the result must say so."""
        result = await executor.call(
            ToolRequest(
                tool="platform.campaign_report",
                arguments={
                    "platform": "google",
                    "campaign_external_id": "ext_1",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-07",
                },
                agent=AgentName.MONITOR,
            )
        )

        assert result.data["served_by"] == "mock"

    async def test_an_agent_budget_write_never_reaches_the_adapter(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.set_daily_budget",
                arguments={
                    "platform": "meta",
                    "campaign_external_id": "ext_1",
                    "daily_budget": 250.0,
                    "reason": "burn rate is 3x the daily budget",
                },
                agent=AgentName.OPTIMIZE,
                run_id="run_1",
            )
        )

        assert result.outcome is ToolOutcome.DRY_RUN
        assert result.data["arguments"]["daily_budget"] == 250.0
        assert client.calls == []

    async def test_an_operator_budget_write_reaches_the_adapter(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.set_daily_budget",
                arguments={
                    "platform": "meta",
                    "campaign_external_id": "ext_1",
                    "daily_budget": 250.456,
                    "reason": "approved by an operator",
                },
                actor="operator:u1",
            )
        )

        assert result.outcome is ToolOutcome.SUCCESS
        assert result.data["success"] is True
        assert result.data["requested_by"] == "operator:u1"
        assert client.calls == [
            {
                "operation": "update_budget",
                "sequence": 1,
                "external_id": "ext_1",
                "daily_budget": 250.46,
            }
        ]

    async def test_a_pause_is_recorded_with_its_reason(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        await executor.call(
            ToolRequest(
                tool="platform.pause_campaign",
                arguments={
                    "platform": "tiktok",
                    "campaign_external_id": "ext_9",
                    "reason": "spent 1510% of its daily budget",
                },
                actor="operator:u1",
            )
        )

        assert client.calls[0]["operation"] == "pause_campaign"
        assert client.calls[0]["reason"] == "spent 1510% of its daily budget"

    async def test_a_creative_is_built_from_the_arguments(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        await executor.call(
            ToolRequest(
                tool="platform.create_creative",
                arguments={
                    "platform": "meta",
                    "campaign_external_id": "ext_1",
                    "headline": "Half the price, twice the range",
                    "description": "A tested line from the winning variant.",
                    "cta_text": "Shop Now",
                },
                actor="operator:u1",
            )
        )

        assert client.calls[0]["operation"] == "create_creative"
        assert client.calls[0]["cta_text"] == "Shop Now"

    async def test_a_non_positive_budget_is_refused(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.set_daily_budget",
                arguments={
                    "platform": "meta",
                    "campaign_external_id": "ext_1",
                    "daily_budget": 0,
                    "reason": "zero out the spend",
                },
                actor="operator:u1",
            )
        )

        assert result.outcome is ToolOutcome.VALIDATION_FAILED
        assert client.calls == []

    async def test_a_malformed_date_is_refused(self, executor: ToolExecutor) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.campaign_report",
                arguments={
                    "platform": "google",
                    "campaign_external_id": "ext_1",
                    "start_date": "last monday",
                    "end_date": "2026-09-07",
                },
                agent=AgentName.MONITOR,
            )
        )

        assert result.outcome is ToolOutcome.VALIDATION_FAILED

    async def test_an_unregistered_platform_fails_cleanly(
        self, bus: RecordingBus, sink: RecordingSink
    ) -> None:
        """Warehouse mode with no adapter must error, not silently fall back."""
        platforms = PlatformRegistry({Platform.MOCK: MockAdsClient()}, data_mode=DataMode.WAREHOUSE)
        executor = make_executor(ToolRegistry(build_platform_tools(platforms)), bus, sink)

        result = await executor.call(
            ToolRequest(
                tool="platform.campaign_report",
                arguments={
                    "platform": "google",
                    "campaign_external_id": "ext_1",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-07",
                },
                agent=AgentName.MONITOR,
            )
        )

        assert result.outcome is ToolOutcome.FAILED
        assert result.error is not None and "No adapter registered" in result.error

    async def test_a_resume_reaches_the_adapter(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        await executor.call(
            ToolRequest(
                tool="platform.resume_campaign",
                arguments={
                    "platform": "google",
                    "campaign_external_id": "ext_2",
                    "reason": "burn rate is back inside the daily budget",
                },
                actor="operator:u1",
            )
        )

        assert client.calls[0]["operation"] == "resume_campaign"
        assert client.calls[0]["external_id"] == "ext_2"

    async def test_creative_status_changes_reach_the_adapter(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        for tool in ("platform.pause_creative", "platform.resume_creative"):
            await executor.call(
                ToolRequest(
                    tool=tool,
                    arguments={
                        "platform": "meta",
                        "creative_external_id": "cre_1",
                        "reason": "score below the 40.0 threshold",
                    },
                    actor="operator:u1",
                )
            )

        assert [call["operation"] for call in client.calls] == [
            "pause_creative",
            "resume_creative",
        ]

    async def test_a_creative_status_change_is_dry_run_for_an_agent(
        self, executor: ToolExecutor, client: MockAdsClient
    ) -> None:
        result = await executor.call(
            ToolRequest(
                tool="platform.pause_creative",
                arguments={
                    "platform": "meta",
                    "creative_external_id": "cre_1",
                    "reason": "score below the 40.0 threshold",
                },
                agent=AgentName.CREATIVE,
                run_id="run_1",
            )
        )

        assert result.outcome is ToolOutcome.DRY_RUN
        assert client.calls == []

    async def test_the_whole_catalogue_is_present(self, executor: ToolExecutor) -> None:
        assert set(executor.registry.names()) == PLATFORM_TOOL_NAMES


class TestExecutorAssembly:
    """`build_tool_executor` is what the composition root calls."""

    async def test_the_default_catalogue_is_the_platform_set(self) -> None:
        executor = build_tool_executor(_mock_registry(), ToolSettings())
        assert set(executor.registry.names()) == PLATFORM_TOOL_NAMES

    async def test_extra_specs_extend_the_catalogue(self, spy: Spy) -> None:
        """Domain tools can be contributed without editing the platform module."""
        extra = [
            ToolSpec(
                name="demo.custom",
                description="A tool contributed by the caller.",
                parameters=EchoArgs,
                handler=spy.read,
                read_only=True,
            )
        ]
        executor = build_tool_executor(_mock_registry(), ToolSettings(), extra=extra)

        assert set(executor.registry.names()) == PLATFORM_TOOL_NAMES | {"demo.custom"}
        result = await executor.call(
            ToolRequest(tool="demo.custom", arguments={"value": "ok"}, agent=AgentName.MONITOR)
        )
        assert result.outcome is ToolOutcome.SUCCESS
        assert result.data == {"echo": "ok"}

    async def test_the_bus_is_threaded_through(self, spy: Spy) -> None:
        bus = RecordingBus()
        extra = [
            ToolSpec(
                name="demo.custom",
                description="A tool contributed by the caller.",
                parameters=EchoArgs,
                handler=spy.read,
                read_only=True,
            )
        ]
        executor = build_tool_executor(_mock_registry(), ToolSettings(), bus=bus, extra=extra)
        await executor.call(
            ToolRequest(tool="demo.custom", arguments={"value": "ok"}, run_id="run_1")
        )

        assert bus.published[0]["event_type"] == "tool.invoked"

    async def test_the_guardrails_come_from_settings(self) -> None:
        executor = build_tool_executor(
            _mock_registry(),
            ToolSettings(dry_run=True, allow_agent_writes=True, max_calls_per_run=3),
        )
        usage = executor.usage("run_unseen")

        assert executor.dry_run_by_default is True
        assert usage == {
            "calls": 0,
            "invocations": 0,
            "budget": 3,
            "by_outcome": {},
            "by_tool": {},
            "writes": 0,
            "dry_runs": 0,
            "refusals": 0,
            "failures": 0,
            "dry_run_by_default": True,
            "agent_writes_allowed": True,
            "tools_registered": len(PLATFORM_TOOL_NAMES),
        }
