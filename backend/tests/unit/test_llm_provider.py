"""The OpenAI-compatible provider: endpoint routing, auth and failure mapping.

This is the only code that talks to a paid model vendor, so the tests assert the
exact URL, headers and payload it would send, and that every vendor failure mode
is translated into a domain error the gateway can retry or degrade on.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from adoptimizer.core.config import LLMProvider, LLMSettings
from adoptimizer.core.errors import ExternalServiceError
from adoptimizer.llm.base import CompletionRequest, estimate_cost
from adoptimizer.llm.openai_provider import DEFAULT_BASE_URLS, OpenAICompatibleModel


def make_request(**overrides: Any) -> CompletionRequest:
    payload: dict[str, Any] = {
        "task": "creative_generation",
        "system_prompt": "You are an ad copywriter.",
        "user_prompt": "Write three headlines.",
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


def wire(model: OpenAICompatibleModel, handler: Any) -> list[httpx.Request]:
    """Attach a recording mock transport, bypassing the real network."""
    captured: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    model._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return captured


def ok_handler(
    text: str = '{"headlines": ["a"]}',
    *,
    prompt_tokens: int = 120,
    completion_tokens: int = 45,
    model_name: str | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(
            200,
            json={
                "model": model_name or "gpt-4o-mini",
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            },
        )

    return handler


def settings(**overrides: Any) -> LLMSettings:
    payload: dict[str, Any] = {
        "provider": LLMProvider.OPENAI,
        "model": "gpt-4o-mini",
        "api_key": "sk-test-key",
        "timeout_seconds": 5.0,
    }
    payload.update(overrides)
    return LLMSettings(**payload)


class TestEndpointRouting:
    def test_openai_uses_the_documented_default_base_url(self) -> None:
        model = OpenAICompatibleModel(settings())

        assert model._endpoint() == "https://api.openai.com/v1/chat/completions"

    def test_a_custom_base_url_wins_and_its_trailing_slash_is_stripped(self) -> None:
        model = OpenAICompatibleModel(settings(api_base="https://ollama.internal/v1/"))

        assert model._endpoint() == "https://ollama.internal/v1/chat/completions"

    def test_a_compatible_provider_without_a_base_url_is_a_configuration_error(self) -> None:
        """An empty endpoint would silently post to the process working dir."""
        model = OpenAICompatibleModel(settings(provider=LLMProvider.OPENAI_COMPATIBLE))

        with pytest.raises(ExternalServiceError) as caught:
            model._endpoint()

        assert "LLM__API_BASE is required" in str(caught.value)
        assert DEFAULT_BASE_URLS[LLMProvider.OPENAI_COMPATIBLE] == ""

    def test_azure_targets_the_deployment_endpoint(self) -> None:
        model = OpenAICompatibleModel(
            settings(
                provider=LLMProvider.AZURE_OPENAI,
                api_base="https://contoso.openai.azure.com",
                model="gpt-4.1-mini",
                api_version="2025-03-01",
            )
        )

        assert model._endpoint() == (
            "https://contoso.openai.azure.com/openai/deployments/gpt-4-1-mini"
            "/chat/completions?api-version=2025-03-01"
        )

    def test_azure_falls_back_to_a_pinned_api_version(self) -> None:
        model = OpenAICompatibleModel(
            settings(provider=LLMProvider.AZURE_OPENAI, api_base="https://contoso.openai.azure.com")
        )

        assert "api-version=2024-10-21" in model._endpoint()

    def test_dots_in_a_deployment_name_are_replaced_for_azure(self) -> None:
        """Azure deployment names cannot contain dots."""
        model = OpenAICompatibleModel(
            settings(provider=LLMProvider.AZURE_OPENAI, api_base="https://x", model="gpt-4.1")
        )

        assert "/deployments/gpt-4-1/" in model._endpoint()


class TestAuthentication:
    def test_openai_sends_a_bearer_token(self) -> None:
        model = OpenAICompatibleModel(settings())

        assert model._headers()["Authorization"] == "Bearer sk-test-key"
        assert "api-key" not in model._headers()

    def test_azure_sends_the_api_key_header_instead(self) -> None:
        model = OpenAICompatibleModel(settings(provider=LLMProvider.AZURE_OPENAI))

        headers = model._headers()
        assert headers["api-key"] == "sk-test-key"
        assert "Authorization" not in headers

    def test_the_organization_header_is_only_sent_when_configured(self) -> None:
        assert "OpenAI-Organization" not in OpenAICompatibleModel(settings())._headers()

        with_org = OpenAICompatibleModel(settings(organization="org_123"))
        assert with_org._headers()["OpenAI-Organization"] == "org_123"

    def test_a_configured_key_makes_the_provider_available(self) -> None:
        assert OpenAICompatibleModel(settings()).is_available is True

    @pytest.mark.parametrize(
        "provider",
        [LLMProvider.OPENAI, LLMProvider.AZURE_OPENAI, LLMProvider.OPENAI_COMPATIBLE],
    )
    def test_a_hosted_provider_without_a_key_is_rejected_at_configuration_time(
        self, provider: LLMProvider
    ) -> None:
        """The runtime guard below is defence in depth; this is the real gate."""
        with pytest.raises(ValidationError) as caught:
            settings(provider=provider, api_key="")

        assert "LLM__API_KEY is required" in str(caught.value)

    def test_the_mock_provider_needs_no_credentials(self) -> None:
        assert LLMSettings(provider=LLMProvider.MOCK).api_key.get_secret_value() == ""

    async def test_a_provider_that_lost_its_key_refuses_before_sending(self) -> None:
        model = OpenAICompatibleModel(settings())
        model.is_available = False
        captured = wire(model, ok_handler())

        with pytest.raises(ExternalServiceError) as caught:
            await model.complete(make_request())

        assert "no API key configured" in str(caught.value)
        assert captured == []


class TestCompletion:
    async def test_text_usage_and_model_are_mapped_from_the_response(self) -> None:
        model = OpenAICompatibleModel(settings())
        wire(model, ok_handler("hello", prompt_tokens=11, completion_tokens=7))

        result = await model.complete(make_request())

        assert result.text == "hello"
        assert result.usage.prompt_tokens == 11
        assert result.usage.completion_tokens == 7
        assert result.provider is LLMProvider.OPENAI
        assert result.model == "gpt-4o-mini"
        assert result.latency_ms >= 0
        assert result.degraded is False
        assert result.cached is False

    async def test_cost_is_estimated_from_the_pricing_table(self) -> None:
        model = OpenAICompatibleModel(settings())
        wire(model, ok_handler(prompt_tokens=1_000_000, completion_tokens=1_000_000))

        result = await model.complete(make_request())

        assert result.usage.cost_usd == pytest.approx(estimate_cost("gpt-4o-mini", 10**6, 10**6))
        assert result.usage.cost_usd > 0

    async def test_the_provider_model_name_wins_over_the_request(self) -> None:
        """Vendors alias deployments; billing reports must match the vendor."""
        model = OpenAICompatibleModel(settings())
        wire(model, ok_handler(model_name="gpt-4o-mini-2024-07-18"))

        result = await model.complete(make_request())

        assert result.model == "gpt-4o-mini-2024-07-18"

    async def test_missing_usage_counts_as_zero_rather_than_failing(self) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        wire(model, handler)

        result = await model.complete(make_request())

        assert result.usage.total_tokens == 0
        assert result.usage.cost_usd == 0.0

    async def test_a_null_usage_block_is_tolerated(self) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}], "usage": None}
            )

        wire(model, handler)

        assert (await model.complete(make_request())).usage.prompt_tokens == 0

    async def test_the_payload_carries_both_prompts_and_the_settings_defaults(self) -> None:
        model = OpenAICompatibleModel(settings(temperature=0.2, max_tokens=512))
        captured = wire(model, ok_handler())

        await model.complete(make_request())

        body = json.loads(captured[0].content)
        assert body["model"] == "gpt-4o-mini"
        assert body["messages"] == [
            {"role": "system", "content": "You are an ad copywriter."},
            {"role": "user", "content": "Write three headlines."},
        ]
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 512
        assert body["response_format"] == {"type": "json_object"}

    async def test_a_request_overrides_the_configured_defaults(self) -> None:
        model = OpenAICompatibleModel(settings(temperature=0.2, max_tokens=512))
        captured = wire(model, ok_handler())

        await model.complete(make_request(temperature=0.9, max_tokens=64))

        body = json.loads(captured[0].content)
        assert body["temperature"] == 0.9
        assert body["max_tokens"] == 64

    async def test_plain_text_mode_omits_the_json_response_format(self) -> None:
        model = OpenAICompatibleModel(settings())
        captured = wire(model, ok_handler())

        await model.complete(make_request(json_mode=False))

        assert "response_format" not in json.loads(captured[0].content)

    async def test_a_zero_max_tokens_falls_back_to_the_setting(self) -> None:
        model = OpenAICompatibleModel(settings(max_tokens=256))
        captured = wire(model, ok_handler())

        await model.complete(make_request(max_tokens=0))

        assert json.loads(captured[0].content)["max_tokens"] == 256


class TestFailureMapping:
    @pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
    async def test_an_http_error_becomes_a_domain_error_carrying_the_body(
        self, status: int
    ) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(status, text="upstream said no")

        wire(model, handler)

        with pytest.raises(ExternalServiceError) as caught:
            await model.complete(make_request())

        assert "returned " + str(status) in str(caught.value)
        assert caught.value.detail is not None
        assert caught.value.detail["status"] == status
        assert "upstream said no" in str(caught.value.detail["body"])

    async def test_a_long_error_body_is_truncated(self) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(500, text="e" * 5000)

        wire(model, handler)

        with pytest.raises(ExternalServiceError) as caught:
            await model.complete(make_request())

        assert caught.value.detail is not None
        assert len(str(caught.value.detail["body"])) == 400

    async def test_an_empty_choice_list_is_refused(self) -> None:
        """A 200 with no content must not be handed to a JSON parser."""
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(200, json={"choices": []})

        wire(model, handler)

        with pytest.raises(ExternalServiceError) as caught:
            await model.complete(make_request())

        assert "no choices" in str(caught.value)

    async def test_a_missing_choices_key_is_refused(self) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(200, json={"id": "chatcmpl-1"})

        wire(model, handler)

        with pytest.raises(ExternalServiceError):
            await model.complete(make_request())

    async def test_a_null_message_content_yields_empty_text(self) -> None:
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

        wire(model, handler)

        assert (await model.complete(make_request())).text == ""

    @pytest.mark.parametrize(
        "exception",
        [httpx.ConnectError("dns"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("t")],
    )
    async def test_transport_failures_are_wrapped_not_leaked(
        self, exception: httpx.HTTPError
    ) -> None:
        """The gateway retries on ExternalServiceError only."""
        model = OpenAICompatibleModel(settings())

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            raise exception

        wire(model, handler)

        with pytest.raises(ExternalServiceError) as caught:
            await model.complete(make_request())

        assert "LLM request failed" in str(caught.value)


class TestClientLifecycle:
    async def test_the_client_is_created_once_and_reused(self) -> None:
        model = OpenAICompatibleModel(settings())
        captured = wire(model, ok_handler())
        first = model._client

        await model.complete(make_request())
        await model.complete(make_request())

        assert model._client is first
        assert len(captured) == 2

    async def test_a_closed_client_is_replaced(self) -> None:
        model = OpenAICompatibleModel(settings())
        await model.close()
        assert model._client is None

        rebuilt = await model._ensure_client()
        assert rebuilt is not None and not rebuilt.is_closed
        await model.close()

    async def test_close_is_idempotent(self) -> None:
        model = OpenAICompatibleModel(settings())
        wire(model, ok_handler())

        await model.close()
        await model.close()

        assert model._client is None
