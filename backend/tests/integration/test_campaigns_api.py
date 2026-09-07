"""Campaign, creative and metric endpoints over HTTP."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, access_token, bearer

CAMPAIGNS = API + "/campaigns"
SEEDED_CAMPAIGNS = 8

NEW_CAMPAIGN: dict[str, Any] = {
    "name": "Nimbus Retargeting Q4",
    "platform": "meta",
    "daily_budget": 750.0,
    "total_budget": 20_000.0,
    "target_cpa": 85.0,
    "target_roas": 2.5,
    "objective": "conversions",
    "target_audience": "Cart abandoners, 25-44",
}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


async def create(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: Any
) -> httpx.Response:
    return await client.post(CAMPAIGNS, json={**NEW_CAMPAIGN, **overrides}, headers=headers)


class TestSeeding:
    async def test_a_fresh_database_is_seeded(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        assert page["total"] == SEEDED_CAMPAIGNS
        assert len(page["items"]) == SEEDED_CAMPAIGNS

    async def test_seeded_campaigns_have_delivery_data(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        metrics = await client.get(CAMPAIGNS + "/" + campaign_id + "/metrics", headers=admin)
        assert metrics.status_code == 200
        assert metrics.json()


class TestListing:
    async def test_envelope_shape(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        body = (await client.get(CAMPAIGNS, headers=admin)).json()
        assert set(body) == {"items", "total", "page", "page_size"}

    async def test_pagination(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        first = (
            await client.get(CAMPAIGNS, params={"page": 1, "page_size": 3}, headers=admin)
        ).json()
        second = (
            await client.get(CAMPAIGNS, params={"page": 2, "page_size": 3}, headers=admin)
        ).json()
        assert len(first["items"]) == 3
        assert len(second["items"]) == 3
        assert first["total"] == second["total"] == SEEDED_CAMPAIGNS
        assert {c["id"] for c in first["items"]}.isdisjoint({c["id"] for c in second["items"]})

    async def test_page_size_is_bounded(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        assert (
            await client.get(CAMPAIGNS, params={"page_size": 5_000}, headers=admin)
        ).status_code == 422
        assert (await client.get(CAMPAIGNS, params={"page": 0}, headers=admin)).status_code == 422

    async def test_platform_filter(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        body = (await client.get(CAMPAIGNS, params={"platform": "tiktok"}, headers=admin)).json()
        assert body["items"]
        assert all(c["platform"] == "tiktok" for c in body["items"])

    async def test_unknown_platform_is_rejected(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        assert (
            await client.get(CAMPAIGNS, params={"platform": "myspace"}, headers=admin)
        ).status_code == 422

    async def test_status_filter(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        body = (await client.get(CAMPAIGNS, params={"status": "active"}, headers=admin)).json()
        assert all(c["status"] == "active" for c in body["items"])

    async def test_search_filter(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        everything = (await client.get(CAMPAIGNS, headers=admin)).json()
        needle = everything["items"][0]["name"][:6]
        body = (await client.get(CAMPAIGNS, params={"search": needle}, headers=admin)).json()
        assert body["total"] >= 1
        assert all(needle.lower() in c["name"].lower() for c in body["items"])

    async def test_search_with_no_match_is_empty(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (
            await client.get(CAMPAIGNS, params={"search": "zzz-no-such-campaign"}, headers=admin)
        ).json()
        assert body["total"] == 0
        assert body["items"] == []


class TestCreate:
    async def test_created_with_supplied_values(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await create(client, admin)
        assert response.status_code == 201
        body = response.json()
        assert body["id"].startswith("camp")
        assert body["name"] == NEW_CAMPAIGN["name"]
        assert body["platform"] == "meta"
        assert body["daily_budget"] == 750.0
        assert body["target_roas"] == 2.5
        assert body["status"] == "active"

    async def test_defaults_are_applied(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.post(
            CAMPAIGNS,
            json={"name": "Minimal Campaign", "platform": "mock", "daily_budget": 100.0},
            headers=admin,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["target_cpa"] == 100.0
        assert body["target_roas"] == 2.0
        assert body["objective"] == "conversions"

    async def test_creation_is_listed_afterwards(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        created = (await create(client, admin)).json()
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        assert page["total"] == SEEDED_CAMPAIGNS + 1
        assert any(c["id"] == created["id"] for c in page["items"])

    async def test_creation_is_audited(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        created = (await create(client, admin)).json()
        page = (
            await client.get(
                API + "/admin/audit",
                params={"action": "campaign.created", "resource_type": "campaign"},
                headers=admin,
            )
        ).json()
        assert any(entry["resource_id"] == created["id"] for entry in page["items"])

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"name": "x"}, "name"),
            ({"daily_budget": 0}, "daily_budget"),
            ({"daily_budget": -10.0}, "daily_budget"),
            ({"target_cpa": 0}, "target_cpa"),
            ({"target_roas": 0}, "target_roas"),
            ({"platform": "myspace"}, "platform"),
        ],
    )
    async def test_invalid_payloads_are_rejected(
        self,
        client: httpx.AsyncClient,
        admin: dict[str, str],
        overrides: dict[str, Any],
        field: str,
    ) -> None:
        response = await create(client, admin, **overrides)
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_failed"
        assert field in str(body["errors"])

    async def test_missing_required_fields_are_rejected(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        assert (await client.post(CAMPAIGNS, json={}, headers=admin)).status_code == 422


class TestReadUpdateDelete:
    async def test_get_one(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.get(CAMPAIGNS + "/" + campaign_id, headers=admin)
        assert response.status_code == 200
        assert response.json()["id"] == campaign_id

    async def test_get_missing_is_a_problem_document(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(CAMPAIGNS + "/camp_missing", headers=admin)
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404
        assert body["request_id"]

    async def test_partial_update(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.patch(
            CAMPAIGNS + "/" + campaign_id, json={"daily_budget": 999.0}, headers=admin
        )
        assert response.status_code == 200
        body = response.json()
        assert body["daily_budget"] == 999.0
        # Untouched fields keep their value.
        assert body["name"] == page["items"][0]["name"]

    async def test_pause_and_resume(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        paused = await client.patch(
            CAMPAIGNS + "/" + campaign_id, json={"status": "paused"}, headers=admin
        )
        assert paused.json()["status"] == "paused"
        resumed = await client.patch(
            CAMPAIGNS + "/" + campaign_id, json={"status": "active"}, headers=admin
        )
        assert resumed.json()["status"] == "active"

    async def test_update_records_before_and_after(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        await client.patch(
            CAMPAIGNS + "/" + campaign_id, json={"daily_budget": 1_234.0}, headers=admin
        )
        audit = (
            await client.get(
                API + "/admin/audit", params={"action": "campaign.updated"}, headers=admin
            )
        ).json()
        entry = next(e for e in audit["items"] if e["resource_id"] == campaign_id)
        assert entry["before"]["daily_budget"] != 1_234.0
        assert entry["after"]["daily_budget"] == 1_234.0

    async def test_invalid_update_is_rejected(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.patch(
            CAMPAIGNS + "/" + campaign_id, json={"daily_budget": -1}, headers=admin
        )
        assert response.status_code == 422

    async def test_delete_removes_the_campaign(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        created = (await create(client, admin)).json()
        response = await client.delete(CAMPAIGNS + "/" + created["id"], headers=admin)
        assert response.status_code in {200, 204}
        assert (await client.get(CAMPAIGNS + "/" + created["id"], headers=admin)).status_code == 404

    async def test_delete_missing_is_404(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        assert (await client.delete(CAMPAIGNS + "/camp_missing", headers=admin)).status_code == 404


class TestCreatives:
    async def test_list_creatives_for_a_campaign(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.get(CAMPAIGNS + "/" + campaign_id + "/creatives", headers=admin)
        assert response.status_code == 200
        # A campaign's creatives are returned as a bare list, not a page envelope:
        # the collection is bounded by the campaign and needs no pagination.
        body = response.json()
        assert isinstance(body, list)
        assert body

    async def test_create_a_creative(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.post(
            CAMPAIGNS + "/" + campaign_id + "/creatives",
            json={
                "headline": "Ship faster without the surprises",
                "description": "Deterministic delivery windows, no fine print.",
                "cta_text": "See how",
                "creative_type": "text",
                "ab_group": "variant_a",
                "origin": "human",
            },
            headers=admin,
        )
        assert response.status_code in {200, 201}, response.text
        body = response.json()
        assert body["campaign_id"] == campaign_id
        assert body["status"] == "draft"

    async def test_creative_headline_length_is_enforced(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        response = await client.post(
            CAMPAIGNS + "/" + campaign_id + "/creatives", json={"headline": "hi"}, headers=admin
        )
        assert response.status_code == 422

    async def test_creative_status_transition(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        page = (await client.get(CAMPAIGNS, headers=admin)).json()
        campaign_id = page["items"][0]["id"]
        creatives = (
            await client.get(CAMPAIGNS + "/" + campaign_id + "/creatives", headers=admin)
        ).json()
        creative_id = creatives[0]["id"]
        response = await client.patch(
            CAMPAIGNS + "/" + campaign_id + "/creatives/" + creative_id,
            json={"status": "paused"},
            headers=admin,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paused"

    async def test_creative_status_is_scoped_to_the_campaign_that_owns_it(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        """The campaign id in the path must actually authorise the edit.

        Regression guard: the handler used to pass only ``creative_id`` down to
        the service, so the campaign segment was decorative and any caller with
        ``creative:write`` could pause another campaign's creative by naming an
        unrelated campaign in the URL.
        """
        page = (await client.get(CAMPAIGNS, params={"page_size": 2}, headers=admin)).json()
        owner_id = page["items"][0]["id"]
        foreign_id = page["items"][1]["id"]
        assert owner_id != foreign_id
        creatives = (
            await client.get(CAMPAIGNS + "/" + owner_id + "/creatives", headers=admin)
        ).json()
        creative_id = creatives[0]["id"]

        response = await client.patch(
            CAMPAIGNS + "/" + foreign_id + "/creatives/" + creative_id,
            json={"status": "paused"},
            headers=admin,
        )

        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "not_found"

        unchanged = (
            await client.get(CAMPAIGNS + "/" + owner_id + "/creatives", headers=admin)
        ).json()
        current = [item["status"] for item in unchanged if item["id"] == creative_id]
        assert current == [creatives[0]["status"]]

    async def test_creative_listing_endpoint(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/creatives", params={"page_size": 5}, headers=admin)).json()
        assert body["total"] > 0
        assert len(body["items"]) <= 5

    async def test_creative_summary(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        response = await client.get(API + "/creatives/summary", headers=admin)
        assert response.status_code == 200
        assert isinstance(response.json(), dict)
