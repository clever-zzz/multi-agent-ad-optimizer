"""HTTP-level tests for the three real platform adapters.

Every other adapter test in this suite runs against ``StubRegistry`` and
``MockAdsClient``. Those prove the *dispatch* is wired correctly - that the
right client is selected and the right method called - but they say nothing
about the bytes a real call would put on the wire. This module closes that gap.
An injected ``httpx.MockTransport`` records each request so the path, headers
and payload can be asserted, and because the transport is the only thing
swapped out, the adapter code exercised here is the code that runs in
production.

Writing these tests is what surfaced three defects that no mock-based test
could have caught:

* **TikTok sent no credential at all on writes.** ``_headers()`` existed but
  was never passed to the HTTP client, so every ``_post`` would have been
  rejected with 401.
* **TikTok put the token in the query string on reads**, which leaks the
  credential into access logs, proxy traces and browser history.
* **Meta stringified ``object_story_spec`` as a Python repr** (single quotes)
  rather than JSON, which the Graph API rejects with a 400 - a failure mode
  that looks like a payload problem rather than an encoding bug.

Each fix has a regression test below.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from adoptimizer.core.errors import ExternalServiceError
from adoptimizer.domain.enums import Platform
from adoptimizer.infra.ads.base import CreativeDraft
from adoptimizer.infra.ads.google import GoogleAdsClient, GoogleAdsCredentials
from adoptimizer.infra.ads.http_client import PlatformHTTPClient, observe_latency, with_timeout
from adoptimizer.infra.ads.meta import MetaAdsClient, _meta_cta
from adoptimizer.infra.ads.tiktok import TikTokAdsClient

Responder = Callable[[httpx.Request], httpx.Response]
Captured = list[httpx.Request]


def recording(handler: Responder | httpx.Response) -> tuple[httpx.MockTransport, Captured]:
    """A mock transport that records every request it is handed.

    Accepts either a responder function (when the reply depends on the request)
    or a fixed response (when every call should get the same answer).
    """
    captured: Captured = []

    if isinstance(handler, httpx.Response):
        fixed = handler

        def responder(_request: httpx.Request) -> httpx.Response:
            return fixed

    else:
        responder = handler

    def record(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)

    return httpx.MockTransport(record), captured


def route(routes: dict[str, Responder | httpx.Response]) -> Responder:
    """Dispatch by URL fragment, so one transport can serve OAuth and API calls.

    An unrouted request fails loudly rather than silently returning a default -
    a test that quietly accepts an unexpected call proves nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, responder in routes.items():
            if fragment in url:
                return responder(request) if callable(responder) else responder
        raise AssertionError("unrouted request: " + url)

    return handler


def body_of(request: httpx.Request) -> dict[str, Any]:
    """Parse a JSON request body, asserting it is JSON in the first place."""
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


# --------------------------------------------------------------------------- #
# Google Ads
# --------------------------------------------------------------------------- #

GOOGLE_CREDENTIALS: dict[str, str] = {
    "client_id": "client-123",
    "client_secret": "secret-456",
    "refresh_token": "refresh-789",
    "developer_token": "developer-token-abc",
    # Deliberately dashed: the adapter must strip the separators Google's UI
    # shows before building a resource path.
    "customer_id": "123-456-7890",
}

TOKEN_PAYLOAD: dict[str, Any] = {"access_token": "ya29.access", "expires_in": 3600}


def google_client(
    routes: dict[str, Responder | httpx.Response],
) -> tuple[GoogleAdsClient, Captured]:
    transport, captured = recording(route(routes))
    credentials = GoogleAdsCredentials(**GOOGLE_CREDENTIALS, transport=transport)
    return GoogleAdsClient(credentials), captured


def google_routes(**extra: Responder | httpx.Response) -> dict[str, Responder | httpx.Response]:
    routes: dict[str, Responder | httpx.Response] = {
        "oauth2.googleapis.com/token": httpx.Response(200, json=TOKEN_PAYLOAD)
    }
    routes.update(extra)
    return routes


class TestGoogleAdsAdapter:
    async def test_unconfigured_client_refuses_before_touching_the_network(self) -> None:
        credentials = GoogleAdsCredentials(
            client_id="",
            client_secret="",
            refresh_token="",
            developer_token="",
            customer_id="",
        )
        client = GoogleAdsClient(credentials)

        assert client.is_configured is False
        with pytest.raises(ExternalServiceError, match="not configured"):
            await client.pause_campaign("1", reason="r")

    async def test_budget_update_sends_micros_and_both_required_headers(self) -> None:
        client, captured = google_client(
            google_routes(
                **{
                    "campaignBudgets:mutate": httpx.Response(
                        200,
                        json={
                            "results": [
                                {"resourceName": "customers/1234567890/campaignBudgets/999"}
                            ]
                        },
                    )
                }
            )
        )

        result = await client.update_campaign_budget("999", daily_budget=12.34)

        assert result.success is True
        assert result.external_reference == "customers/1234567890/campaignBudgets/999"

        token_call, mutate_call = captured
        assert str(token_call.url) == "https://oauth2.googleapis.com/token"
        assert b"grant_type=refresh_token" in token_call.content
        assert b"client_id=client-123" in token_call.content

        assert mutate_call.method == "POST"
        assert str(mutate_call.url) == (
            "https://googleads.googleapis.com/v18/customers/1234567890/campaignBudgets:mutate"
        )
        # Both headers are mandatory for Google Ads; omitting either is a 401.
        assert mutate_call.headers["authorization"] == "Bearer ya29.access"
        assert mutate_call.headers["developer-token"] == "developer-token-abc"

        operations = body_of(mutate_call)["operations"]
        # 12.34 must arrive as integer micros, as a string.
        assert operations[0]["update"]["amountMicros"] == "12340000"
        assert operations[0]["updateMask"] == "amountMicros"

    async def test_access_token_is_cached_across_calls(self) -> None:
        client, captured = google_client(
            google_routes(
                **{
                    "campaigns:mutate": httpx.Response(
                        200,
                        json={"results": [{"resourceName": "customers/1234567890/campaigns/1"}]},
                    )
                }
            )
        )

        await client.pause_campaign("1", reason="a")
        await client.resume_campaign("1", reason="b")

        token_calls = [request for request in captured if "oauth2" in str(request.url)]
        assert len(token_calls) == 1, "a second OAuth round trip would waste a request per action"

    async def test_campaign_status_uses_the_documented_google_enums(self) -> None:
        client, captured = google_client(
            google_routes(
                **{
                    "campaigns:mutate": httpx.Response(
                        200,
                        json={"results": [{"resourceName": "customers/1234567890/campaigns/1"}]},
                    )
                }
            )
        )

        await client.pause_campaign("1", reason="underperforming")
        await client.resume_campaign("1", reason="recovered")

        mutate_calls = [request for request in captured if "mutate" in str(request.url)]
        assert len(mutate_calls) == 2
        # Google uses ENABLED, not ACTIVE. Sending ACTIVE is a 400.
        assert body_of(mutate_calls[0])["operations"][0]["update"]["status"] == "PAUSED"
        assert body_of(mutate_calls[1])["operations"][0]["update"]["status"] == "ENABLED"

    async def test_creative_status_targets_ad_group_ads(self) -> None:
        client, captured = google_client(
            google_routes(
                **{
                    "adGroupAds:mutate": httpx.Response(
                        200,
                        json={"results": [{"resourceName": "customers/1234567890/adGroupAds/1~2"}]},
                    )
                }
            )
        )

        await client.pause_creative("1~2", reason="fatigued")

        mutate_call = captured[-1]
        assert "adGroupAds:mutate" in str(mutate_call.url)
        assert body_of(mutate_call)["operations"][0]["update"]["resourceName"] == ("adGroupAds/1~2")

    async def test_report_query_carries_the_date_window_and_normalises_cost(self) -> None:
        client, captured = google_client(
            google_routes(
                **{
                    "googleAds:search": httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "metrics": {
                                        "impressions": "100",
                                        "clicks": "10",
                                        "conversions": "2.0",
                                        "costMicros": "3500000",
                                    },
                                    "segments": {"date": "2026-09-01"},
                                }
                            ]
                        },
                    )
                }
            )
        )

        payload = await client.fetch_report("999", start_date="2026-09-01", end_date="2026-09-07")

        assert payload["source"] == "google"
        # Micros are converted to currency at the boundary, exactly once.
        assert payload["rows"] == [
            {
                "date": "2026-09-01",
                "impressions": 100,
                "clicks": 10,
                "conversions": 2,
                "cost": 3.5,
            }
        ]

        query = body_of(captured[-1])["query"]
        assert "campaign.id = 999" in query
        assert "BETWEEN '2026-09-01' AND '2026-09-07'" in query

    async def test_creating_a_creative_is_refused_with_actionable_guidance(self) -> None:
        client, _ = google_client(google_routes())

        with pytest.raises(ExternalServiceError, match="ad group"):
            await client.create_creative("1", CreativeDraft(headline="h", description="d"))

    async def test_close_is_idempotent(self) -> None:
        client, _ = google_client(google_routes())

        await client.close()
        await client.close()


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


def meta_client(
    responder: Responder | httpx.Response,
    *,
    access_token: str = "meta-token",
    ad_account_id: str = "act_123",
) -> tuple[MetaAdsClient, Captured]:
    transport, captured = recording(responder)
    return (
        MetaAdsClient(access_token=access_token, ad_account_id=ad_account_id, transport=transport),
        captured,
    )


class TestMetaAdsAdapter:
    async def test_unconfigured_client_refuses_before_touching_the_network(self) -> None:
        client = MetaAdsClient(access_token="", ad_account_id="")

        assert client.is_configured is False
        with pytest.raises(ExternalServiceError, match="not configured"):
            await client.pause_campaign("1", reason="r")

    async def test_access_token_is_never_placed_in_the_query_string(self) -> None:
        """Regression: every Meta call used to append ``access_token`` to the URL.

        Graph API accepts that spelling, so nothing failed - the credential just
        landed in access logs and proxy traces, the same leak TikTok had on
        reads. All five operations are walked because reverting any one call
        site is enough to bring it back, and a test that checked a single one
        would not notice.
        """
        client, captured = meta_client(
            httpx.Response(200, json={"success": True, "id": "cr_1", "data": []}),
            access_token="super-secret-token",
        )

        await client.update_campaign_budget("c_1", daily_budget=12.34)
        await client.pause_campaign("c_1", reason="r")
        await client.resume_campaign("c_1", reason="r")
        await client.create_creative("page_1", CreativeDraft(headline="h", description="d"))
        await client.fetch_report("c_1", start_date="2026-09-01", end_date="2026-09-07")

        assert len(captured) == 5
        for request in captured:
            assert request.headers["authorization"] == "Bearer super-secret-token"
            assert "super-secret-token" not in str(request.url)
            assert "access_token" not in request.url.params

    async def test_budget_is_sent_as_integer_cents(self) -> None:
        client, captured = meta_client(httpx.Response(200, json={"success": True}))

        result = await client.update_campaign_budget("c_1", daily_budget=12.34)

        assert result.external_reference == "c_1"
        request = captured[0]
        assert request.method == "POST"
        assert str(request.url.copy_with(query=None)) == "https://graph.facebook.com/v21.0/c_1"
        # Meta bills in minor units; sending 12.34 would mean twelve cents.
        assert request.url.params["daily_budget"] == "1234"
        assert request.headers["authorization"] == "Bearer meta-token"

    async def test_ad_account_prefix_is_added_exactly_once(self) -> None:
        client, captured = meta_client(
            httpx.Response(200, json={"id": "cr_1"}), ad_account_id="123"
        )

        await client.create_creative("page_1", CreativeDraft(headline="h", description="d"))

        assert "/act_123/adcreatives" in str(captured[0].url)

    async def test_creative_spec_is_json_encoded_not_a_python_repr(self) -> None:
        """Regression: httpx happily stringifies a dict as a Python repr.

        ``{'page_id': 'p'}`` is not valid JSON, so Meta answers 400 and the
        failure looks like a bad payload rather than a bad encoding.
        """
        client, captured = meta_client(httpx.Response(200, json={"id": "cr_1"}))

        await client.create_creative(
            "page_1",
            CreativeDraft(
                headline="Big sale",
                description="Everything must go",
                cta_text="Buy now",
                asset_urls={"link": "https://shop.example.com"},
            ),
        )

        raw = captured[0].url.params["object_story_spec"]
        spec = json.loads(raw)  # must parse, or the test fails here
        assert spec["page_id"] == "page_1"
        assert spec["link_data"]["message"] == "Everything must go"
        assert spec["link_data"]["call_to_action"]["type"] == "SHOP_NOW"
        # The giveaway of a Python repr is the single quote.
        assert '"page_id"' in raw

    async def test_status_updates_use_meta_enums(self) -> None:
        client, captured = meta_client(httpx.Response(200, json={"success": True}))

        await client.pause_campaign("c_1", reason="r")
        await client.resume_campaign("c_1", reason="r")

        assert captured[0].url.params["status"] == "PAUSED"
        # Meta uses ACTIVE, unlike Google's ENABLED.
        assert captured[1].url.params["status"] == "ACTIVE"

    async def test_report_rolls_purchase_actions_into_conversions(self) -> None:
        client, captured = meta_client(
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "date_start": "2026-09-01",
                            "impressions": "1000",
                            "clicks": "25",
                            "spend": "12.3456",
                            "actions": [
                                {"action_type": "purchase", "value": "3"},
                                {"action_type": "landing_page_view", "value": "40"},
                                {
                                    "action_type": "offsite_conversion.fb_pixel_purchase",
                                    "value": "1",
                                },
                            ],
                        }
                    ]
                },
            )
        )

        payload = await client.fetch_report("c_1", start_date="2026-09-01", end_date="2026-09-07")

        assert payload["rows"] == [
            {
                "date": "2026-09-01",
                "impressions": 1000,
                "clicks": 25,
                # Only the two purchase actions count; the page view must not.
                "conversions": 4,
                "cost": 12.3456,
            }
        ]
        params = captured[0].url.params
        assert params["time_increment"] == "1"
        assert '"since":"2026-09-01"' in params["time_range"]

    @pytest.mark.parametrize(
        ("copy", "expected"),
        [
            ("Buy now", "SHOP_NOW"),
            ("Learn more", "LEARN_MORE"),
            ("Sign up free", "SIGN_UP"),
            ("Download the app", "MOBILE_APP"),
            ("Contact us", "CONTACT_US"),
            ("Book a demo", "BOOK_NOW"),
            ("something unmapped", "LEARN_MORE"),
        ],
    )
    def test_cta_copy_maps_onto_metas_fixed_enum(self, copy: str, expected: str) -> None:
        assert _meta_cta(copy) == expected


# --------------------------------------------------------------------------- #
# TikTok
# --------------------------------------------------------------------------- #


def tiktok_client(
    responder: Responder | httpx.Response, *, access_token: str = "tiktok-token"
) -> tuple[TikTokAdsClient, Captured]:
    transport, captured = recording(responder)
    return (
        TikTokAdsClient(access_token=access_token, advertiser_id="adv_1", transport=transport),
        captured,
    )


class TestTikTokAdsAdapter:
    async def test_unconfigured_client_refuses_before_touching_the_network(self) -> None:
        client = TikTokAdsClient(access_token="", advertiser_id="")

        assert client.is_configured is False
        with pytest.raises(ExternalServiceError, match="not configured"):
            await client.pause_campaign("1", reason="r")

    async def test_access_token_travels_in_the_header_on_writes(self) -> None:
        """Regression: ``_headers()`` existed but was never wired into the client.

        Without it every write would have been a 401, and no mock-based test
        could tell the difference.
        """
        client, captured = tiktok_client(
            httpx.Response(200, json={"code": 0, "data": {"campaign_id": "c_1"}})
        )

        await client.update_campaign_budget("c_1", daily_budget=12.34)

        request = captured[0]
        assert request.headers["access-token"] == "tiktok-token"
        body = body_of(request)
        assert body["advertiser_id"] == "adv_1"
        assert body["campaign_id"] == "c_1"
        assert body["budget"] == 12.34
        assert body["budget_mode"] == "BUDGET_MODE_DAY"

    async def test_access_token_is_never_placed_in_the_query_string(self) -> None:
        """Regression: a credential in the URL leaks into logs and proxy traces."""
        client, captured = tiktok_client(
            httpx.Response(200, json={"code": 0, "data": {"list": []}}),
            access_token="super-secret-token",
        )

        await client.fetch_report("c_1", start_date="2026-09-01", end_date="2026-09-07")

        request = captured[0]
        assert request.headers["access-token"] == "super-secret-token"
        assert "super-secret-token" not in str(request.url)
        assert request.url.params["data_level"] == "AUCTION_CAMPAIGN"
        assert request.url.params["report_type"] == "BASIC"

    async def test_status_updates_hit_the_documented_endpoints(self) -> None:
        client, captured = tiktok_client(httpx.Response(200, json={"code": 0, "data": {}}))

        await client.pause_campaign("c_1", reason="r")
        await client.resume_campaign("c_1", reason="r")
        await client.pause_creative("ad_1", reason="r")

        campaign_calls = [request for request in captured if "campaign/status" in str(request.url)]
        ad_calls = [request for request in captured if "ad/status" in str(request.url)]
        assert body_of(campaign_calls[0])["operation"] == "DISABLE"
        assert body_of(campaign_calls[1])["operation"] == "ENABLE"
        assert body_of(ad_calls[0])["ad_ids"] == ["ad_1"]

    async def test_an_api_level_error_code_becomes_a_domain_error(self) -> None:
        """TikTok returns HTTP 200 with a failure ``code``; that must not pass."""
        client, _ = tiktok_client(
            httpx.Response(200, json={"code": 40001, "message": "Invalid access token"})
        )

        with pytest.raises(ExternalServiceError, match="40001"):
            await client.pause_campaign("c_1", reason="r")

    async def test_report_parses_the_nested_dimensions_block(self) -> None:
        client, _ = tiktok_client(
            httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "dimensions": {"stat_time_day": "2026-09-01 00:00:00"},
                                "metrics": {
                                    "impressions": "500",
                                    "clicks": "20",
                                    "conversion": "4",
                                    "spend": "9.5",
                                },
                            }
                        ]
                    },
                },
            )
        )

        payload = await client.fetch_report("c_1", start_date="2026-09-01", end_date="2026-09-07")

        assert payload["rows"] == [
            {
                "date": "2026-09-01 00:00:00",
                "impressions": 500,
                "clicks": 20,
                "conversions": 4,
                "cost": 9.5,
            }
        ]

    async def test_creating_a_creative_is_refused_with_actionable_guidance(self) -> None:
        client, _ = tiktok_client(httpx.Response(200, json={"code": 0, "data": {}}))

        with pytest.raises(ExternalServiceError, match="video asset"):
            await client.create_creative("c_1", CreativeDraft(headline="h", description="d"))


# --------------------------------------------------------------------------- #
# Shared HTTP plumbing
# --------------------------------------------------------------------------- #


class TestPlatformHTTPClient:
    async def test_a_retryable_status_is_retried_and_can_succeed(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, json={"ok": True})

        transport, captured = recording(handler)
        client = PlatformHTTPClient("https://example.test", transport=transport)

        assert await client.request("GET", "/x") == {"ok": True}
        assert len(captured) == 2, "a 429 must be retried exactly once here"

    async def test_a_client_error_is_not_retried(self) -> None:
        transport, captured = recording(httpx.Response(401, text="unauthorised"))
        client = PlatformHTTPClient("https://example.test", transport=transport)

        with pytest.raises(ExternalServiceError) as excinfo:
            await client.request("GET", "/x")

        assert excinfo.value.detail["status"] == 401
        assert len(captured) == 1, "retrying a 401 just burns quota"

    async def test_a_non_json_body_is_reported_as_such(self) -> None:
        transport, _ = recording(httpx.Response(200, text="<html>gateway timeout</html>"))
        client = PlatformHTTPClient("https://example.test", transport=transport)

        with pytest.raises(ExternalServiceError, match="non-JSON"):
            await client.request("GET", "/x")

    async def test_an_empty_body_is_an_empty_mapping(self) -> None:
        transport, _ = recording(httpx.Response(204))
        client = PlatformHTTPClient("https://example.test", transport=transport)

        assert await client.request("DELETE", "/x") == {}

    async def test_configured_headers_ride_along_on_every_request(self) -> None:
        transport, captured = recording(httpx.Response(200, json={}))
        client = PlatformHTTPClient(
            "https://example.test", headers={"Access-Token": "tok"}, transport=transport
        )

        await client.request("GET", "/x")

        assert captured[0].headers["access-token"] == "tok"
        assert captured[0].headers["accept"] == "application/json"

    async def test_close_releases_the_client_and_can_run_twice(self) -> None:
        transport, _ = recording(httpx.Response(200, json={}))
        client = PlatformHTTPClient("https://example.test", transport=transport)

        await client.request("GET", "/x")
        await client.close()
        await client.close()

    async def test_with_timeout_passes_the_value_through(self) -> None:
        async def quick() -> str:
            return "done"

        assert await with_timeout(quick(), 1.0) == "done"

    def test_observe_latency_labels_the_ads_provider(self) -> None:
        # Shares the LLM histogram with an "ads:" prefix so platform latency is
        # visible without a second metric definition.
        observe_latency("google", 0.25)


class TestRegistryRouting:
    def test_mock_mode_routes_every_platform_to_the_mock_adapter(self) -> None:
        from adoptimizer.core.config import DataMode
        from adoptimizer.infra.ads.mock import MockAdsClient
        from adoptimizer.infra.ads.registry import PlatformRegistry

        registry = PlatformRegistry(
            {
                Platform.MOCK: MockAdsClient(Platform.MOCK),
                Platform.GOOGLE: MockAdsClient(Platform.GOOGLE),
            },
            data_mode=DataMode.MOCK,
        )

        assert isinstance(registry.for_platform(Platform.GOOGLE), MockAdsClient)
        assert registry.data_mode is DataMode.MOCK
