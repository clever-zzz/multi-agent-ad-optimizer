"""The gateway every agent calls: budget, retries, cache and degradation.

These are the guardrails that stop a model outage or a runaway loop from
becoming an invoice, so each one is asserted on its own rather than through an
end-to-end run that could pass while a guard is silently disabled.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from adoptimizer.core.config import LLMProvider, LLMSettings
from adoptimizer.core.errors import BudgetExceededError, ExternalServiceError
from adoptimizer.core.metrics import REGISTRY
from adoptimizer.infra.cache import CacheService, InMemoryCache
from adoptimizer.llm import gateway as gateway_module
from adoptimizer.llm.base import CompletionRequest, CompletionResult, Usage
from adoptimizer.llm.gateway import LLMGateway, NullSpendLedger, build_gateway
from adoptimizer.llm.mock import MockLanguageModel
from adoptimizer.llm.openai_provider import OpenAICompatibleModel
from adoptimizer.llm.structured import StructuredOutputError


class Headline(BaseModel):
    """The smallest schema that proves structured parsing is wired up."""

    text: str
    score: float = 0.0


class FakeModel:
    """A LanguageModel whose failures and latency the test controls."""

    def __init__(
        self,
        *,
        text: str = '{"text": "generated"}',
        fail_times: int = 0,
        error: str = "transient provider failure",
        provider: LLMProvider = LLMProvider.OPENAI,
        model: str = "gpt-4o-mini",
        available: bool = True,
        delay: float = 0.0,
        cost_usd: float = 0.01,
    ) -> None:
        self.text = text
        self.fail_times = fail_times
        self.error = error
        self.provider = provider
        self.model = model
        self.is_available = available
        self.delay = delay
        self.cost_usd = cost_usd
        self.calls = 0
        self.closed = False
        self.in_flight = 0
        self.peak_in_flight = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        _ = request
        self.calls += 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.calls <= self.fail_times:
                raise ExternalServiceError(self.error)
            return CompletionResult(
                text=self.text,
                usage=Usage(prompt_tokens=100, completion_tokens=40, cost_usd=self.cost_usd),
                provider=self.provider,
                model=self.model,
                latency_ms=5,
            )
        finally:
            self.in_flight -= 1

    async def close(self) -> None:
        self.closed = True


class FakeLedger:
    """Reports a fixed month-to-date spend and records what it was told."""

    def __init__(self, spent_usd: float = 0.0) -> None:
        self.spent_usd = spent_usd
        self.queries = 0
        self.records: list[dict[str, Any]] = []

    async def record(self, *, run_id: str | None, agent: str, result: CompletionResult) -> None:
        self.records.append({"run_id": run_id, "agent": agent, "cost_usd": result.usage.cost_usd})

    async def month_to_date_usd(self) -> float:
        self.queries += 1
        return self.spent_usd


def make_settings(**overrides: Any) -> LLMSettings:
    payload: dict[str, Any] = {
        "provider": LLMProvider.OPENAI,
        "model": "gpt-4o-mini",
        "api_key": "sk-test",
        "max_retries": 1,
        "retry_base_delay_seconds": 0.001,
        "concurrency": 4,
        "cache_enabled": False,
        "monthly_budget_usd": 0.0,
    }
    payload.update(overrides)
    return LLMSettings(**payload)


def make_request(prompt: str = "Write three headlines.") -> CompletionRequest:
    return CompletionRequest(task="creative_generation", system_prompt="sys", user_prompt=prompt)


@pytest.fixture
def cache() -> CacheService:
    return CacheService(InMemoryCache(), default_ttl=60)


class TestSpendBudget:
    async def test_a_completion_under_the_cap_is_allowed(self) -> None:
        model = FakeModel()
        ledger = FakeLedger(spent_usd=1.0)
        gateway = LLMGateway(model, settings=make_settings(monthly_budget_usd=10.0), ledger=ledger)

        result = await gateway.complete(make_request())

        assert result.text == '{"text": "generated"}'
        assert ledger.queries == 1

    async def test_spending_at_the_cap_hard_stops_the_run(self) -> None:
        """The cap is a ceiling, not a suggestion: at the limit it refuses."""
        model = FakeModel()
        gateway = LLMGateway(
            model,
            settings=make_settings(monthly_budget_usd=10.0),
            ledger=FakeLedger(spent_usd=10.0),
        )

        with pytest.raises(BudgetExceededError) as caught:
            await gateway.complete(make_request())

        assert "$10.00" in str(caught.value)
        assert model.calls == 0

    async def test_spending_above_the_cap_is_refused_too(self) -> None:
        gateway = LLMGateway(
            FakeModel(),
            settings=make_settings(monthly_budget_usd=10.0),
            ledger=FakeLedger(spent_usd=42.5),
        )

        with pytest.raises(BudgetExceededError):
            await gateway.complete(make_request())

    async def test_the_latch_stays_tripped_so_the_ledger_is_not_polled_again(self) -> None:
        """Once the budget is gone, re-checking spend cannot un-ring the bell."""
        model = FakeModel()
        ledger = FakeLedger(spent_usd=99.0)
        gateway = LLMGateway(model, settings=make_settings(monthly_budget_usd=10.0), ledger=ledger)

        for _ in range(3):
            with pytest.raises(BudgetExceededError):
                await gateway.complete(make_request())

        assert ledger.queries == 1
        assert model.calls == 0

    async def test_a_zero_cap_disables_the_guard_entirely(self) -> None:
        """0 means "unmetered", so the ledger must not even be consulted."""
        ledger = FakeLedger(spent_usd=1_000_000.0)
        gateway = LLMGateway(
            FakeModel(), settings=make_settings(monthly_budget_usd=0.0), ledger=ledger
        )

        assert (await gateway.complete(make_request())).text

        assert ledger.queries == 0

    async def test_eighty_percent_of_the_cap_warns_but_does_not_block(self) -> None:
        model = FakeModel()
        gateway = LLMGateway(
            model,
            settings=make_settings(monthly_budget_usd=10.0),
            ledger=FakeLedger(spent_usd=8.0),
        )

        assert (await gateway.complete(make_request())).text == '{"text": "generated"}'

    async def test_just_under_the_warning_threshold_is_silent(self) -> None:
        gateway = LLMGateway(
            FakeModel(),
            settings=make_settings(monthly_budget_usd=10.0),
            ledger=FakeLedger(spent_usd=7.99),
        )

        assert await gateway.complete(make_request())


class TestRetryAndDegradation:
    async def test_a_transient_failure_is_retried_until_it_succeeds(self) -> None:
        model = FakeModel(fail_times=2)
        gateway = LLMGateway(model, settings=make_settings(max_retries=3))

        result = await gateway.complete(make_request())

        assert model.calls == 3
        assert result.degraded is False
        assert result.error is None

    async def test_backoff_doubles_and_is_capped_at_eight_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uncapped exponential backoff would stall a run for minutes."""
        delays: list[float] = []

        async def record_sleep(seconds: float) -> None:
            delays.append(seconds)

        monkeypatch.setattr(gateway_module.asyncio, "sleep", record_sleep)
        model = FakeModel(fail_times=10)
        gateway = LLMGateway(
            model,
            settings=make_settings(max_retries=4, retry_base_delay_seconds=4.0),
        )

        result = await gateway.complete(make_request())

        assert delays == [4.0, 8.0, 8.0]
        assert result.degraded is True

    async def test_zero_retries_still_makes_one_attempt(self) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(max_retries=0))

        assert (await gateway.complete(make_request())).text

        assert model.calls == 1

    async def test_an_empty_completion_is_treated_as_a_failure(self) -> None:
        """A 200 with no text would otherwise reach the JSON parser."""
        model = FakeModel(text="   ")
        gateway = LLMGateway(model, settings=make_settings(max_retries=2))

        result = await gateway.complete(make_request())

        assert model.calls == 2
        assert result.degraded is True
        assert "empty completion" in str(result.error)

    async def test_degrading_to_the_mock_keeps_the_run_alive(self) -> None:
        model = FakeModel(fail_times=99)
        gateway = LLMGateway(model, settings=make_settings(max_retries=2, fail_open_to_mock=True))

        result = await gateway.complete(make_request())

        assert result.degraded is True
        assert result.provider is LLMProvider.MOCK
        assert result.model == MockLanguageModel.model
        assert "transient provider failure" in str(result.error)
        assert result.text

    async def test_fail_closed_raises_instead_of_substituting_the_mock(self) -> None:
        """Some tenants must never serve model output they did not pay for."""
        model = FakeModel(fail_times=99)
        gateway = LLMGateway(model, settings=make_settings(max_retries=3, fail_open_to_mock=False))

        with pytest.raises(ExternalServiceError) as caught:
            await gateway.complete(make_request())

        assert "failed after 3 attempts" in str(caught.value)
        assert caught.value.detail is not None
        assert "transient provider failure" in str(caught.value.detail["error"])
        assert model.calls == 3

    async def test_the_call_counter_records_the_outcome(self) -> None:
        model = FakeModel(fail_times=1)
        gateway = LLMGateway(model, settings=make_settings(max_retries=2))

        await gateway.complete(make_request())

        assert (
            REGISTRY.get_sample_value(
                "llm_calls_total",
                {"provider": "openai", "model": "gpt-4o-mini", "outcome": "error"},
            )
            or 0.0
        ) >= 1.0

    async def test_a_paid_completion_moves_the_token_and_spend_counters(self) -> None:
        model = FakeModel(cost_usd=0.25)
        gateway = LLMGateway(model, settings=make_settings())
        before = (
            REGISTRY.get_sample_value(
                "llm_spend_usd_total", {"provider": "openai", "model": "gpt-4o-mini"}
            )
            or 0.0
        )

        await gateway.complete(make_request())

        after = (
            REGISTRY.get_sample_value(
                "llm_spend_usd_total", {"provider": "openai", "model": "gpt-4o-mini"}
            )
            or 0.0
        )
        assert after - before == pytest.approx(0.25)
        assert (
            REGISTRY.get_sample_value(
                "llm_tokens_total", {"provider": "openai", "model": "gpt-4o-mini", "kind": "prompt"}
            )
            or 0.0
        ) >= 100.0


class TestConcurrency:
    async def test_the_semaphore_caps_how_many_calls_reach_the_provider(self) -> None:
        """A burst over 200 campaigns must not open 200 sockets."""
        model = FakeModel(delay=0.01)
        gateway = LLMGateway(model, settings=make_settings(concurrency=2))

        await asyncio.gather(*(gateway.complete(make_request("p" + str(i))) for i in range(8)))

        assert model.calls == 8
        assert model.peak_in_flight <= 2


class TestResponseCache:
    async def test_a_repeated_prompt_is_served_from_cache(self, cache: CacheService) -> None:
        model = FakeModel()
        gateway = LLMGateway(
            model, settings=make_settings(cache_enabled=True, cache_ttl_seconds=60), cache=cache
        )

        first = await gateway.complete(make_request())
        second = await gateway.complete(make_request())

        assert model.calls == 1
        assert second.cached is True
        assert second.text == first.text
        # A cache hit is not billed again, so it must not report spend.
        assert second.usage.cost_usd == 0.0
        assert second.latency_ms == 0

    async def test_a_different_prompt_is_a_cache_miss(self, cache: CacheService) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(cache_enabled=True), cache=cache)

        await gateway.complete(make_request("headlines for shoes"))
        await gateway.complete(make_request("headlines for coffee"))

        assert model.calls == 2

    def test_the_cache_key_covers_everything_that_changes_the_answer(self) -> None:
        base = make_request()

        assert base.cache_key == make_request().cache_key
        assert base.cache_key != make_request("different prompt").cache_key
        assert (
            base.cache_key
            != CompletionRequest(
                task="other_task", system_prompt=base.system_prompt, user_prompt=base.user_prompt
            ).cache_key
        )
        assert (
            base.cache_key
            != CompletionRequest(
                task=base.task,
                system_prompt="different system",
                user_prompt=base.user_prompt,
            ).cache_key
        )
        assert (
            base.cache_key
            != CompletionRequest(
                task=base.task,
                system_prompt=base.system_prompt,
                user_prompt=base.user_prompt,
                schema_hint="Headline[]",
            ).cache_key
        )
        assert (
            base.cache_key
            != CompletionRequest(
                task=base.task,
                system_prompt=base.system_prompt,
                user_prompt=base.user_prompt,
                json_mode=False,
            ).cache_key
        )

    async def test_a_disabled_cache_always_calls_the_provider(self, cache: CacheService) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(cache_enabled=False), cache=cache)

        await gateway.complete(make_request())
        await gateway.complete(make_request())

        assert model.calls == 2
        assert await cache.get_json("llm:" + make_request().cache_key) is None

    async def test_caching_enabled_without_a_cache_object_is_a_no_op(self) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(cache_enabled=True), cache=None)

        await gateway.complete(make_request())
        await gateway.complete(make_request())

        assert model.calls == 2

    async def test_a_degraded_result_is_never_cached(self, cache: CacheService) -> None:
        """Caching a fallback answer would serve mock copy for the whole TTL."""
        model = FakeModel(fail_times=99)
        gateway = LLMGateway(
            model, settings=make_settings(cache_enabled=True, max_retries=1), cache=cache
        )

        degraded = await gateway.complete(make_request())
        assert degraded.degraded is True

        model.fail_times = 0
        recovered = await gateway.complete(make_request())

        assert model.calls == 2
        assert recovered.cached is False
        assert recovered.provider is LLMProvider.OPENAI

    async def test_a_corrupt_cache_entry_is_ignored_and_refreshed(
        self, cache: CacheService
    ) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(cache_enabled=True), cache=cache)
        await cache.set_json("llm:" + make_request().cache_key, {"unexpected": True}, 60)

        result = await gateway.complete(make_request())

        assert model.calls == 1
        assert result.cached is False
        assert result.text == '{"text": "generated"}'

    async def test_an_unknown_provider_in_the_cache_is_ignored(self, cache: CacheService) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings(cache_enabled=True), cache=cache)
        await cache.set_json(
            "llm:" + make_request().cache_key,
            {"text": "stale", "provider": "not-a-provider", "model": "x", "usage": {}},
            60,
        )

        result = await gateway.complete(make_request())

        assert model.calls == 1
        assert result.cached is False

    async def test_a_successful_completion_is_written_back_with_its_usage(
        self, cache: CacheService
    ) -> None:
        gateway = LLMGateway(
            FakeModel(),
            settings=make_settings(cache_enabled=True, cache_ttl_seconds=90),
            cache=cache,
        )

        await gateway.complete(make_request())

        stored = await cache.get_json("llm:" + make_request().cache_key)
        assert stored is not None
        assert stored["text"] == '{"text": "generated"}'
        assert stored["provider"] == "openai"
        assert stored["model"] == "gpt-4o-mini"
        assert stored["usage"]["prompt_tokens"] == 100


class TestStructuredGeneration:
    async def test_generate_validates_a_single_object(self) -> None:
        model = FakeModel(text='{"text": "Limited time", "score": 0.8}')
        gateway = LLMGateway(model, settings=make_settings())

        result = await gateway.generate(Headline, make_request())

        assert isinstance(result, Headline)
        assert result.text == "Limited time"
        assert result.score == 0.8

    async def test_generate_many_returns_a_validated_list(self) -> None:
        model = FakeModel(text='[{"text": "a"}, {"text": "b", "score": 0.5}]')
        gateway = LLMGateway(model, settings=make_settings())

        result = await gateway.generate(Headline, make_request(), many=True)

        assert isinstance(result, list)
        assert [item.text for item in result] == ["a", "b"]

    async def test_generate_many_caps_how_many_items_are_parsed(self) -> None:
        """A model that returns 500 variants must not become 500 creatives."""
        model = FakeModel(text="[" + ",".join(['{"text": "h"}'] * 5) + "]")
        gateway = LLMGateway(model, settings=make_settings())

        result = await gateway.generate(Headline, make_request(), many=True, max_items=2)

        assert isinstance(result, list)
        assert len(result) == 2

    async def test_generate_many_unwraps_a_json_envelope(self) -> None:
        model = FakeModel(text='{"items": [{"text": "a"}, {"text": "b"}]}')
        gateway = LLMGateway(model, settings=make_settings())

        result = await gateway.generate(Headline, make_request(), many=True)

        assert isinstance(result, list)
        assert len(result) == 2

    async def test_generate_raises_when_the_output_is_not_json(self) -> None:
        model = FakeModel(text="Sure! Here are three headlines for you.")
        gateway = LLMGateway(model, settings=make_settings())

        with pytest.raises(StructuredOutputError):
            await gateway.generate(Headline, make_request())

    async def test_generate_raises_when_the_schema_does_not_match(self) -> None:
        model = FakeModel(text='{"score": "not a number"}')
        gateway = LLMGateway(model, settings=make_settings())

        with pytest.raises(StructuredOutputError):
            await gateway.generate(Headline, make_request())

    async def test_generate_does_not_swallow_the_budget_guard(self) -> None:
        gateway = LLMGateway(
            FakeModel(),
            settings=make_settings(monthly_budget_usd=5.0),
            ledger=FakeLedger(spent_usd=5.0),
        )

        with pytest.raises(BudgetExceededError):
            await gateway.generate(Headline, make_request())


class TestAccounting:
    async def test_the_null_ledger_accumulates_spend_and_tokens(self) -> None:
        ledger = NullSpendLedger()

        await ledger.record(
            run_id="run_1",
            agent="creative",
            result=CompletionResult(
                text="x",
                usage=Usage(prompt_tokens=10, completion_tokens=5, cost_usd=0.25),
                provider=LLMProvider.MOCK,
                model="mock",
                latency_ms=1,
            ),
        )

        assert ledger.total_usd == 0.25
        assert ledger.total_tokens == 15
        assert await ledger.month_to_date_usd() == 0.25

    async def test_the_ledger_is_told_which_run_and_agent_spent(self) -> None:
        ledger = FakeLedger()
        gateway = LLMGateway(FakeModel(cost_usd=0.5), settings=make_settings(), ledger=ledger)

        await gateway.complete(make_request(), run_id="run_42", agent="bidding")

        assert ledger.records == [{"run_id": "run_42", "agent": "bidding", "cost_usd": 0.5}]

    async def test_a_gateway_without_a_ledger_still_accounts_locally(self) -> None:
        gateway = LLMGateway(FakeModel(cost_usd=0.1), settings=make_settings())

        await gateway.complete(make_request())

        ledger = gateway._ledger
        assert isinstance(ledger, NullSpendLedger)
        assert ledger.total_usd == pytest.approx(0.1)
        assert await ledger.month_to_date_usd() == pytest.approx(0.1)


class TestIdentityAndLifecycle:
    def test_the_gateway_reports_its_primary_provider(self) -> None:
        gateway = LLMGateway(FakeModel(), settings=make_settings())

        assert gateway.provider is LLMProvider.OPENAI
        assert gateway.model == "gpt-4o-mini"
        assert gateway.is_live is True

    def test_the_mock_provider_is_never_reported_as_live(self) -> None:
        gateway = LLMGateway(MockLanguageModel(), settings=make_settings())

        assert gateway.is_live is False
        assert gateway.provider is LLMProvider.MOCK

    def test_a_real_provider_without_a_key_is_not_live(self) -> None:
        """Readiness must not claim a paid provider that cannot be called."""
        gateway = LLMGateway(FakeModel(available=False), settings=make_settings())

        assert gateway.is_live is False

    async def test_close_releases_the_primary_model(self) -> None:
        model = FakeModel()
        gateway = LLMGateway(model, settings=make_settings())

        await gateway.close()

        assert model.closed is True


class TestBuildGateway:
    def test_the_mock_provider_needs_no_credentials(self) -> None:
        gateway = build_gateway(LLMSettings(provider=LLMProvider.MOCK))

        assert isinstance(gateway._primary, MockLanguageModel)
        assert gateway.is_live is False

    def test_a_hosted_provider_builds_the_openai_compatible_client(self) -> None:
        gateway = build_gateway(
            LLMSettings(provider=LLMProvider.OPENAI, api_key="sk-live", model="gpt-4o")
        )

        assert isinstance(gateway._primary, OpenAICompatibleModel)
        assert gateway.is_live is True
        assert gateway.model == "gpt-4o"

    def test_the_cache_and_ledger_are_threaded_through(self) -> None:
        cache = CacheService(InMemoryCache())
        ledger = FakeLedger()

        gateway = build_gateway(LLMSettings(provider=LLMProvider.MOCK), cache=cache, ledger=ledger)

        assert gateway._cache is cache
        assert gateway._ledger is ledger
