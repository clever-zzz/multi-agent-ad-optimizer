"""Ingestion over HTTP.

The unit tests prove the rules are right; these prove the wiring is. What has to
hold end to end: the endpoint is authorised, a dirty feed produces a report
rather than an exception or a 4xx, provenance actually reaches the column, an
absent column leaves the stored number alone, and every attempt - including a
rehearsal that wrote nothing - is recorded where an operator can find it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from adoptimizer.core.clock import utc_today
from adoptimizer.infra.db.models import DailyMetric
from adoptimizer.services.ingest import IngestService

from ..conftest import ADMIN_EMAIL, API

INGEST = API + "/ingest"
FEED = "test.feed"
OTHER_FEED = "other.feed"


def day(offset: int = 0) -> str:
    """An ISO date ``offset`` days before today."""
    return (utc_today() - timedelta(days=offset)).isoformat()


def payload(
    records: list[dict[str, Any]], *, source: str = FEED, dry_run: bool = False
) -> dict[str, Any]:
    return {"source": source, "dry_run": dry_run, "records": records}


async def metric_rows(app: FastAPI, campaign_id: str) -> list[DailyMetric]:
    """Read the stored aggregates straight back out of the database."""
    async with app.state.container.database.unit_of_work() as session:
        statement = select(DailyMetric).where(
            DailyMetric.campaign_id == campaign_id, DailyMetric.creative_id.is_(None)
        )
        return list((await session.execute(statement)).scalars().all())


async def push(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    records: list[dict[str, Any]],
    *,
    source: str = FEED,
    dry_run: bool = False,
) -> dict[str, Any]:
    """POST a batch and assert it was processed, then hand back the report."""
    response = await client.post(
        INGEST + "/metrics", json=payload(records, source=source, dry_run=dry_run), headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def campaign(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> dict[str, Any]:
    """A campaign with platform coordinates, so both identity schemes are testable."""
    created = await client.post(
        API + "/campaigns",
        json={
            "name": "Ingest Probe",
            "platform": "mock",
            "daily_budget": 500.0,
            "external_id": "ext_ingest_probe",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
async def rival(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> dict[str, Any]:
    """A second campaign, so a misattributed creative has somewhere wrong to go."""
    created = await client.post(
        API + "/campaigns",
        json={"name": "Ingest Rival", "platform": "mock", "daily_budget": 250.0},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    return created.json()


@pytest.fixture
async def creative(
    client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
) -> dict[str, Any]:
    added = await client.post(
        API + "/campaigns/" + campaign["id"] + "/creatives",
        json={"headline": "Ingest probe creative", "description": "d"},
        headers=admin_headers,
    )
    assert added.status_code == 201, added.text
    return added.json()


class TestPush:
    async def test_a_new_slot_is_created(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {
                    "campaign_id": campaign["id"],
                    "stat_date": day(),
                    "impressions": 1000,
                    "cost": 42.5,
                }
            ],
        )

        assert report["received"] == 1
        assert report["created"] == 1
        assert report["updated"] == 0
        assert report["rejected_count"] == 0
        assert report["unresolved_count"] == 0
        assert report["dry_run"] is False
        assert report["batch_id"].startswith("ing_")
        assert report["window_start"] == report["window_end"] == day()

    async def test_repushing_a_day_updates_it_instead_of_duplicating(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        row = {"campaign_id": campaign["id"], "stat_date": day(), "impressions": 1000, "cost": 42.5}
        await push(client, admin_headers, [row])

        second = await push(client, admin_headers, [{**row, "impressions": 2500}])

        assert (second["created"], second["updated"]) == (0, 1)
        rows = await metric_rows(app, campaign["id"])
        assert len(rows) == 1
        assert rows[0].impressions == 2500

    async def test_platform_coordinates_reach_the_same_slot(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """Two addressing schemes, one truth: a feed must not be able to fork a day."""
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "impressions": 10}],
        )

        report = await push(
            client,
            admin_headers,
            [
                {
                    "platform": "mock",
                    "external_id": "ext_ingest_probe",
                    "stat_date": day(),
                    "clicks": 4,
                }
            ],
        )

        assert (report["created"], report["updated"]) == (0, 1)
        rows = await metric_rows(app, campaign["id"])
        assert len(rows) == 1
        assert (rows[0].impressions, rows[0].clicks) == (10, 4)

    async def test_an_unasserted_column_is_left_alone(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """The whole reason measurements are optional.

        An ad network feed that reported revenue as 0 would erase the number a
        commerce feed supplied, and the optimizer would then act on a fabricated
        ROAS of zero. Absent has to mean "not measured", not "nothing happened".
        """
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "cost": 42.5}],
        )
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "revenue": 300.0}],
            source=OTHER_FEED,
        )

        rows = await metric_rows(app, campaign["id"])
        assert len(rows) == 1
        assert rows[0].cost == 42.5
        assert rows[0].revenue == 300.0
        assert rows[0].impressions == 0

    async def test_a_zero_is_still_written(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """Zero is an assertion; only absence is silence."""
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "cost": 42.5}],
        )

        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "cost": 0.0}],
        )

        assert (await metric_rows(app, campaign["id"]))[0].cost == 0.0

    async def test_an_unknown_external_id_is_reported_not_dropped(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {
                    "platform": "mock",
                    "external_id": "ext_never_imported",
                    "stat_date": day(),
                    "clicks": 1,
                }
            ],
        )

        assert (report["created"], report["updated"], report["unresolved_count"]) == (0, 0, 1)
        issue = report["unresolved"][0]
        assert issue["index"] == 0
        assert issue["identity"] == "mock/ext_never_imported"
        assert "no campaign matches mock/ext_never_imported" in issue["reason"]

    async def test_an_unknown_internal_id_is_reported(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": "camp_does_not_exist", "stat_date": day(), "clicks": 1}],
        )

        assert report["unresolved_count"] == 1
        assert "no campaign has id camp_does_not_exist" in report["unresolved"][0]["reason"]

    async def test_disagreeing_identities_are_reported(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        rival: dict[str, Any],
    ) -> None:
        """The producer's mapping has drifted; writing either side would hide it."""
        report = await push(
            client,
            admin_headers,
            [
                {
                    "campaign_id": rival["id"],
                    "platform": "mock",
                    "external_id": "ext_ingest_probe",
                    "stat_date": day(),
                    "clicks": 1,
                }
            ],
        )

        assert (report["created"], report["updated"], report["unresolved_count"]) == (0, 0, 1)
        assert "two different campaigns" in report["unresolved"][0]["reason"]
        assert campaign["id"] in report["unresolved"][0]["reason"]

    async def test_one_bad_row_does_not_take_the_batch_down(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"campaign_id": campaign["id"], "stat_date": day(1), "impressions": 100},
                {"campaign_id": campaign["id"], "stat_date": day(2), "impressions": -100},
                {"campaign_id": campaign["id"], "stat_date": day(3), "impressions": 300},
            ],
        )

        assert (report["created"], report["rejected_count"]) == (2, 1)
        assert report["rejected"][0]["index"] == 1
        assert "negative value for impressions" in report["rejected"][0]["reason"]
        assert len(await metric_rows(app, campaign["id"])) == 2

    async def test_a_record_that_measures_nothing_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client, admin_headers, [{"campaign_id": campaign["id"], "stat_date": day()}]
        )

        assert report["rejected_count"] == 1
        assert "no measurements" in report["rejected"][0]["reason"]

    async def test_a_far_future_date_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(-40), "impressions": 1}],
        )

        assert report["rejected_count"] == 1
        assert "ahead of today" in report["rejected"][0]["reason"]

    async def test_a_batch_that_asserts_one_slot_twice_rejects_both(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"campaign_id": campaign["id"], "stat_date": day(), "impressions": 100},
                {"campaign_id": campaign["id"], "stat_date": day(), "impressions": 900},
            ],
        )

        assert (report["created"], report["updated"], report["rejected_count"]) == (0, 0, 2)
        assert sorted(issue["index"] for issue in report["rejected"]) == [0, 1]
        assert await metric_rows(app, campaign["id"]) == []

    async def test_a_creative_slot_lands_beside_the_campaign_slot(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        creative: dict[str, Any],
        app: FastAPI,
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"campaign_id": campaign["id"], "stat_date": day(), "impressions": 100},
                {
                    "campaign_id": campaign["id"],
                    "creative_id": creative["id"],
                    "stat_date": day(),
                    "impressions": 60,
                },
            ],
        )

        assert report["created"] == 2
        async with app.state.container.database.unit_of_work() as session:
            rows = list(
                (
                    await session.execute(
                        select(DailyMetric).where(DailyMetric.campaign_id == campaign["id"])
                    )
                )
                .scalars()
                .all()
            )
        assert sorted(row.impressions for row in rows) == [60, 100]

    async def test_a_creative_from_another_campaign_is_reported(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        creative: dict[str, Any],
        rival: dict[str, Any],
    ) -> None:
        """Spend under one campaign, breakdown under another, both totals wrong."""
        report = await push(
            client,
            admin_headers,
            [
                {
                    "campaign_id": rival["id"],
                    "creative_id": creative["id"],
                    "stat_date": day(),
                    "impressions": 1,
                }
            ],
        )

        assert report["unresolved_count"] == 1
        assert "belongs to campaign" in report["unresolved"][0]["reason"]

    async def test_an_unknown_creative_is_reported(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {
                    "campaign_id": campaign["id"],
                    "creative_id": "cre_missing",
                    "stat_date": day(),
                    "impressions": 1,
                }
            ],
        )

        assert report["unresolved_count"] == 1
        assert "no creative has id cre_missing" in report["unresolved"][0]["reason"]

    async def test_a_fractional_conversion_count_is_rounded_not_refused(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """Google Ads reports conversions as a double; a strict int would 422 the feed."""
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "conversions": 2.6}],
        )

        assert report["created"] == 1
        assert (await metric_rows(app, campaign["id"]))[0].conversions == 3


class TestRequestValidation:
    """Structural problems fail the request; value problems only fail a row."""

    async def test_a_broken_type_fails_the_whole_request(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        response = await client.post(
            INGEST + "/metrics",
            json=payload([{"campaign_id": campaign["id"], "stat_date": "not-a-date", "clicks": 1}]),
            headers=admin_headers,
        )

        assert response.status_code == 422

    async def test_a_renamed_column_fails_the_whole_request(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        """Silently ignoring "spend" would write cost as zero and look correct."""
        response = await client.post(
            INGEST + "/metrics",
            json=payload([{"campaign_id": campaign["id"], "stat_date": day(), "spend": 4.0}]),
            headers=admin_headers,
        )

        assert response.status_code == 422

    async def test_an_empty_batch_is_refused(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(INGEST + "/metrics", json=payload([]), headers=admin_headers)

        assert response.status_code == 422

    async def test_a_free_text_source_name_is_refused(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        """Source names are a Prometheus label; unbounded text is unbounded cardinality."""
        response = await client.post(
            INGEST + "/metrics",
            json=payload(
                [{"campaign_id": campaign["id"], "stat_date": day(), "clicks": 1}],
                source="A Feed From Somewhere",
            ),
            headers=admin_headers,
        )

        assert response.status_code == 422


class TestProvenance:
    async def test_the_feed_and_the_batch_reach_the_row(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """So a number that looks wrong can be traced to whoever asserted it."""
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "cost": 1.0}],
        )

        row = (await metric_rows(app, campaign["id"]))[0]
        assert row.source == FEED
        assert row.batch_id == report["batch_id"]

    async def test_the_last_feed_to_assert_a_slot_owns_the_stamp(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "cost": 1.0}],
        )

        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "revenue": 2.0}],
            source=OTHER_FEED,
        )

        row = (await metric_rows(app, campaign["id"]))[0]
        assert row.source == OTHER_FEED
        assert row.batch_id == report["batch_id"]


class TestDryRun:
    async def test_nothing_is_written_but_the_attempt_is_recorded(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        campaign: dict[str, Any],
        app: FastAPI,
    ) -> None:
        """A rehearsal must be distinguishable from a feed that never arrived."""
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "impressions": 100}],
            dry_run=True,
        )

        assert (report["dry_run"], report["created"], report["updated"]) == (True, 1, 0)
        assert report["batch_id"] is not None
        assert await metric_rows(app, campaign["id"]) == []

        batches = (await client.get(INGEST + "/batches", headers=admin_headers)).json()
        assert batches["items"][0]["dry_run"] is True
        assert batches["items"][0]["created"] == 1

    async def test_a_dry_run_still_reports_what_would_fail(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"platform": "mock", "external_id": "ext_unknown", "stat_date": day(), "clicks": 1},
                {"campaign_id": "camp_unknown", "stat_date": day(), "impressions": -5},
            ],
            dry_run=True,
        )

        assert (report["rejected_count"], report["unresolved_count"]) == (1, 1)
        assert report["created"] == 0


class TestBatchLedger:
    async def test_attempts_are_listed_newest_first(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        first = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(2), "clicks": 1}],
        )
        second = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(1), "clicks": 1}],
            source=OTHER_FEED,
        )

        page = (await client.get(INGEST + "/batches", headers=admin_headers)).json()

        assert page["total"] == 2
        assert [item["id"] for item in page["items"]] == [second["batch_id"], first["batch_id"]]
        assert page["items"][0]["source"] == OTHER_FEED

    async def test_the_ledger_can_be_narrowed_to_one_feed(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(2), "clicks": 1}],
        )
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(1), "clicks": 1}],
            source=OTHER_FEED,
        )

        page = (
            await client.get(
                INGEST + "/batches", params={"source": OTHER_FEED}, headers=admin_headers
            )
        ).json()

        assert page["total"] == 1
        assert page["items"][0]["source"] == OTHER_FEED

    async def test_the_ledger_counts_match_the_report(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"campaign_id": campaign["id"], "stat_date": day(1), "clicks": 1},
                {
                    "platform": "mock",
                    "external_id": "ext_unknown",
                    "stat_date": day(1),
                    "clicks": 1,
                },
            ],
        )

        entry = (await client.get(INGEST + "/batches", headers=admin_headers)).json()["items"][0]

        assert entry["id"] == report["batch_id"]
        # A count means the same thing on both surfaces, so the two agree field
        # for field without a translation table.
        for field in ("received", "created", "updated", "rejected_count", "unresolved_count"):
            assert entry[field] == report[field], field
        assert entry["actor"] == ADMIN_EMAIL
        assert entry["window_start"] == day(1)

    async def test_the_recorded_window_spans_the_whole_batch(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [
                {"campaign_id": campaign["id"], "stat_date": day(0), "clicks": 1},
                {"campaign_id": campaign["id"], "stat_date": day(9), "clicks": 1},
            ],
        )

        assert (report["window_start"], report["window_end"]) == (day(9), day(0))


class TestSources:
    async def test_the_registered_feeds_are_listed_with_their_readiness(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        body = (await client.get(INGEST + "/sources", headers=admin_headers)).json()

        assert {source["name"] for source in body["sources"]} == {"platform", "synthetic"}
        # Mock mode has no rows to pull, so the platform feed must not claim to work.
        assert body["configured"] == ["synthetic"]
        by_name = {source["name"]: source for source in body["sources"]}
        assert by_name["platform"]["configured"] is False
        assert by_name["synthetic"]["configured"] is True


class TestObservability:
    async def test_the_attempt_is_audited_with_its_counts(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        report = await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "clicks": 1}],
        )

        page = (await client.get(API + "/admin/audit", headers=admin_headers)).json()
        entries = [item for item in page["items"] if item["action"] == "metrics.ingested"]

        assert len(entries) == 1
        assert entries[0]["resource_id"] == report["batch_id"]
        assert entries[0]["actor_email"] == ADMIN_EMAIL
        assert entries[0]["after"]["source"] == FEED
        assert entries[0]["after"]["created"] == 1

    async def test_the_pipeline_identity_reaches_the_audit_trail(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        make_user: Any,
        campaign: dict[str, Any],
    ) -> None:
        # Attributing a batch to the pipeline rather than to whoever's account the
        # cron job borrowed is the reason INGESTOR exists, and actor_role is what
        # makes that attribution readable after the fact.
        ingestor = await make_user("ingestor")
        report = await push(
            client,
            ingestor["headers"],
            [{"campaign_id": campaign["id"], "stat_date": day(), "clicks": 1}],
        )

        page = (await client.get(API + "/admin/audit", headers=admin_headers)).json()
        entry = next(item for item in page["items"] if item["resource_id"] == report["batch_id"])

        assert entry["actor_role"] == "ingestor"
        assert entry["actor_email"] == ingestor["email"]

    async def test_dispositions_reach_prometheus(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], campaign: dict[str, Any]
    ) -> None:
        await push(
            client,
            admin_headers,
            [{"campaign_id": campaign["id"], "stat_date": day(), "clicks": 1}],
        )

        body = (await client.get("/metrics")).text

        assert "ingest_records_total" in body
        assert 'source="' + FEED + '"' in body
        assert "ingest_batches_total" in body


class TestPullService:
    """The scheduled path: pull from a registered feed, same accounting as a push."""

    async def test_the_synthetic_feed_lands_against_the_seeded_portfolio(
        self, app: FastAPI
    ) -> None:
        container = app.state.container
        start = utc_today() - timedelta(days=24)

        async with container.database.unit_of_work() as session:
            report = await IngestService(session).pull(
                container.ingest_sources, "synthetic", start=start, end=utc_today(), actor="pytest"
            )
            await session.commit()

        # The demo seed writes 21 days of campaign-level rows for 8 campaigns, so
        # a 25-day window overwrites those and creates the four older days.
        assert report.received == 8 * 25
        assert report.updated == 8 * 21
        assert report.created == 8 * 4
        assert report.rejected_count == 0
        assert report.unresolved_count == 0
        report.reconciles()

    async def test_an_unknown_feed_is_refused_by_name(self, app: FastAPI) -> None:
        from adoptimizer.core.errors import NotFoundError

        container = app.state.container
        async with container.database.unit_of_work() as session:
            with pytest.raises(NotFoundError):
                await IngestService(session).pull(
                    container.ingest_sources,
                    "google",
                    start=utc_today(),
                    end=utc_today(),
                )

    async def test_only_campaigns_with_platform_coordinates_are_targets(self, app: FastAPI) -> None:
        """A campaign no feed can name is not a pull target."""
        container = app.state.container
        async with container.database.unit_of_work() as session:
            targets = await IngestService(session).targets()

        assert len(targets) == 8
        assert all(target.campaign_id for target in targets)

    async def test_a_blank_platform_id_is_not_a_pull_target(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], app: FastAPI
    ) -> None:
        """Whitespace gets past the repository's ``IS NOT NULL AND <> ''`` filter.

        No feed can address a campaign whose platform id is blank, so it is
        dropped here rather than handed to an adapter that would fail halfway
        through a fetch and take the rest of the window with it.
        """
        created = await client.post(
            API + "/campaigns",
            json={
                "name": "Blank External",
                "platform": "mock",
                "daily_budget": 100.0,
                "external_id": "   ",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text

        container = app.state.container
        async with container.database.unit_of_work() as session:
            targets = await IngestService(session).targets()

        assert created.json()["id"] not in {target.campaign_id for target in targets}
        assert len(targets) == 8
