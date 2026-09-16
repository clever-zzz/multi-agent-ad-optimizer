"""Mirroring daily metrics from the primary datastore into the warehouse.

The mirror exists because the two stores have different jobs: one is
transactional and authoritative, the other is an analytical replica. These tests
pin down the properties that make running it in production safe - it is
idempotent, it never silently reports success over an empty warehouse, and it
carries both granularities across rather than collapsing them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Any

import pytest

from adoptimizer.core.clock import utc_today
from adoptimizer.core.config import DatabaseSettings
from adoptimizer.core.errors import ExternalServiceError
from adoptimizer.domain.enums import CampaignStatus, Platform
from adoptimizer.infra.analytics import DailyMetricRow, NullSink, WriteReport
from adoptimizer.infra.db.session import Database
from adoptimizer.repositories.campaigns import (
    CampaignRepository,
    CreativeRepository,
    MetricRepository,
)
from adoptimizer.services.warehouse_sync import WarehouseSyncService


class RecordingSink:
    """Accepts writes and remembers them, so a mirror run can be asserted on."""

    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.is_available = True
        self.rows: list[DailyMetricRow] = []
        self._fail = fail

    async def write_events(self, events: Sequence[Any]) -> WriteReport:
        _ = events
        return WriteReport(table="ad_events")

    async def write_daily(self, rows: Sequence[DailyMetricRow]) -> WriteReport:
        if self._fail:
            raise ExternalServiceError("warehouse rejected the batch")
        self.rows.extend(rows)
        return WriteReport(table="campaign_daily_metrics", written=len(rows))

    async def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "status": "ok"}

    async def close(self) -> None:
        return None


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await instance.create_all()
    yield instance
    await instance.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Any]:
    async with database.unit_of_work() as unit:
        yield unit


@pytest.fixture
async def seeded(session: Any) -> dict[str, str]:
    """One campaign with a roll-up row, a breakdown row and an out-of-window row."""
    campaigns = CampaignRepository(session)
    creatives = CreativeRepository(session)
    metrics = MetricRepository(session)
    today = utc_today()

    campaign = await campaigns.create(
        name="Alpha",
        platform=Platform.GOOGLE,
        daily_budget=1000.0,
        total_budget=10000.0,
        target_cpa=80.0,
        target_roas=2.0,
        start_date=today - timedelta(days=40),
        status=CampaignStatus.ACTIVE,
    )
    creative = await creatives.create(campaign_id=campaign.id, headline="Hero")

    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today,
        impressions=10_000,
        clicks=400,
        conversions=20,
        cost=500.0,
        revenue=1500.0,
        source="platform",
    )
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today,
        impressions=6_000,
        clicks=300,
        conversions=15,
        cost=300.0,
        revenue=1100.0,
        creative_id=creative.id,
    )
    # Outside a 30-day window; the mirror must not drag it along.
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today - timedelta(days=90),
        impressions=999_999,
        clicks=999_999,
        conversions=999_999,
        cost=999_999.0,
        revenue=999_999.0,
    )
    return {"campaign": campaign.id, "creative": creative.id}


class TestExportDaily:
    async def test_both_granularities_are_carried_across(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        """Collapsing them here would discard the creative breakdown on the way out."""
        rows = await MetricRepository(session).export_daily(days=30)

        assert len(rows) == 2
        creative_ids = sorted(row.creative_id for row in rows)
        assert creative_ids == ["", seeded["creative"]]

    async def test_the_window_bounds_the_export(self, session: Any, seeded: dict[str, str]) -> None:
        rows = await MetricRepository(session).export_daily(days=30)

        assert all(row.stat_date >= utc_today() - timedelta(days=29) for row in rows)
        assert all(row.impressions != 999_999 for row in rows)

    async def test_a_wider_window_reaches_the_old_row(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        rows = await MetricRepository(session).export_daily(days=120)

        assert len(rows) == 3
        assert any(row.impressions == 999_999 for row in rows)

    async def test_the_export_can_be_scoped_to_one_campaign(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        rows = await MetricRepository(session).export_daily(
            days=30, campaign_ids=["cmp_nonexistent"]
        )

        assert rows == []

    async def test_provenance_and_reach_survive_the_round_trip(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        rows = await MetricRepository(session).export_daily(days=30)

        rollup = next(row for row in rows if not row.creative_id)
        assert rollup.source == "platform"
        assert rollup.impressions == 10_000
        assert rollup.cost == 500.0


class TestWarehouseSync:
    async def test_a_sync_mirrors_every_row(self, session: Any, seeded: dict[str, str]) -> None:
        sink = RecordingSink()

        report = await WarehouseSyncService(session, sink).sync(days=30)

        assert report.candidates == 2
        assert report.written == 2
        assert report.ok is True
        assert len(sink.rows) == 2

    async def test_a_dry_run_reports_the_volume_without_writing(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        sink = RecordingSink()

        report = await WarehouseSyncService(session, sink).sync(days=30, dry_run=True)

        assert report.candidates == 2
        assert report.written == 0
        assert report.ok is True
        assert sink.rows == []
        assert report.report.reasons == {"dry_run": 2}

    async def test_an_empty_window_is_a_success_not_a_failure(self, session: Any) -> None:
        """A fresh install has no metrics yet; that is not an error."""
        sink = RecordingSink()

        report = await WarehouseSyncService(session, sink).sync(days=30)

        assert report.candidates == 0
        assert report.written == 0
        assert report.ok is True
        assert sink.rows == []

    async def test_rows_found_but_none_landed_is_reported_as_not_ok(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        """The failure this service exists to make visible."""
        report = await WarehouseSyncService(session, NullSink()).sync(days=30)

        assert report.candidates == 2
        assert report.written == 0
        assert report.ok is False
        assert report.to_dict()["warehouse"]["skipped"] == 2

    async def test_a_sink_failure_propagates(self, session: Any, seeded: dict[str, str]) -> None:
        sink = RecordingSink(fail=True)

        with pytest.raises(ExternalServiceError, match="rejected the batch"):
            await WarehouseSyncService(session, sink).sync(days=30)

    async def test_status_describes_the_configured_sink(self, session: Any) -> None:
        status = await WarehouseSyncService(session, NullSink()).status()

        assert status["sink"] == "null"
        assert status["available"] is False
        assert status["health"]["status"] == "disabled"

    async def test_sync_can_be_scoped_to_one_campaign(
        self, session: Any, seeded: dict[str, str]
    ) -> None:
        sink = RecordingSink()

        report = await WarehouseSyncService(session, sink).sync(
            days=30, campaign_ids=["cmp_nonexistent"]
        )

        assert report.candidates == 0
        assert sink.rows == []
