"""The metrics read model, on both warehouse backends.

Agents score campaigns from whatever this module returns, so a wrong aggregate
silently biases every budget decision. The SQL backend is exercised against a
real (in-memory) database and the ClickHouse backend against a fake driver,
which is the only way to cover its query building without a warehouse.

The dashboard trend is covered here as well. It is a third reader over the same
two-granularity table, so it has to reach the same numbers rather than merely
plausible ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest

from adoptimizer.core.clock import utc_today
from adoptimizer.core.config import ClickHouseSettings, DatabaseSettings
from adoptimizer.domain.enums import CampaignStatus, Platform
from adoptimizer.infra import warehouse as warehouse_module
from adoptimizer.infra.db.session import Database
from adoptimizer.infra.warehouse import (
    MEASURES,
    ClickHouseWarehouse,
    SqlAggregateWarehouse,
    build_warehouse,
)
from adoptimizer.repositories.campaigns import (
    CampaignRepository,
    CreativeRepository,
    MetricRepository,
)
from adoptimizer.services.analytics import AnalyticsService

from ..conftest import make_settings


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
async def portfolio(session: Any) -> dict[str, str]:
    """Two campaigns, one with creative-level rows, one with an old row."""
    campaigns = CampaignRepository(session)
    creatives = CreativeRepository(session)
    metrics = MetricRepository(session)
    today = utc_today()

    alpha = await campaigns.create(
        name="Alpha",
        platform=Platform.MOCK,
        daily_budget=1000.0,
        total_budget=10000.0,
        target_cpa=80.0,
        target_roas=2.0,
        start_date=today - timedelta(days=40),
        status=CampaignStatus.ACTIVE,
    )
    beta = await campaigns.create(
        name="Beta",
        platform=Platform.MOCK,
        daily_budget=500.0,
        total_budget=5000.0,
        target_cpa=60.0,
        target_roas=3.0,
        start_date=today - timedelta(days=40),
        status=CampaignStatus.ACTIVE,
    )
    hero = await creatives.create(campaign_id=alpha.id, headline="Hero")
    backup = await creatives.create(campaign_id=alpha.id, headline="Backup")

    await metrics.upsert_daily(
        campaign_id=alpha.id,
        stat_date=today,
        impressions=10_000,
        clicks=400,
        conversions=20,
        cost=500.0,
        revenue=1500.0,
    )
    await metrics.upsert_daily(
        campaign_id=alpha.id,
        stat_date=today - timedelta(days=1),
        impressions=8_000,
        clicks=240,
        conversions=8,
        cost=400.0,
        revenue=900.0,
    )
    # Outside every window the tests ask for; it must never be summed in.
    await metrics.upsert_daily(
        campaign_id=alpha.id,
        stat_date=today - timedelta(days=60),
        impressions=999_999,
        clicks=999_999,
        conversions=999_999,
        cost=999_999.0,
        revenue=999_999.0,
    )
    await metrics.upsert_daily(
        campaign_id=beta.id,
        stat_date=today,
        impressions=2_000,
        clicks=40,
        conversions=1,
        cost=200.0,
        revenue=300.0,
    )
    await metrics.upsert_daily(
        campaign_id=alpha.id,
        stat_date=today,
        impressions=6_000,
        clicks=300,
        conversions=15,
        cost=300.0,
        revenue=1100.0,
        creative_id=hero.id,
    )
    await metrics.upsert_daily(
        campaign_id=alpha.id,
        stat_date=today,
        impressions=4_000,
        clicks=100,
        conversions=5,
        cost=200.0,
        revenue=400.0,
        creative_id=backup.id,
    )
    return {"alpha": alpha.id, "beta": beta.id, "hero": hero.id, "backup": backup.id}


async def add_breakdown_only_campaign(session: Any) -> str:
    """A campaign ingested per creative, with no campaign-level roll-up row."""
    campaigns = CampaignRepository(session)
    creatives = CreativeRepository(session)
    metrics = MetricRepository(session)
    today = utc_today()

    campaign = await campaigns.create(
        name="Breakdown only",
        platform=Platform.MOCK,
        daily_budget=300.0,
        total_budget=3000.0,
        target_cpa=50.0,
        target_roas=2.5,
        start_date=today - timedelta(days=10),
        status=CampaignStatus.ACTIVE,
    )
    creative = await creatives.create(campaign_id=campaign.id, headline="Only child")
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today,
        impressions=3_000,
        clicks=90,
        conversions=6,
        cost=150.0,
        revenue=420.0,
        creative_id=creative.id,
    )
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today - timedelta(days=1),
        impressions=2_000,
        clicks=50,
        conversions=3,
        cost=100.0,
        revenue=260.0,
        creative_id=creative.id,
    )
    return campaign.id


async def add_inverted_funnel_campaign(session: Any) -> str:
    """A campaign whose stored funnel is inverted, the way two feeds can leave it.

    Ingestion refuses a single record that claims more clicks than impressions,
    but ``upsert_daily`` lets one feed assert impressions and another assert clicks
    on the same slot, and neither record is invalid on its own. This is the shape
    that actually reaches storage, and the one a well-formed fixture never builds.
    """
    campaigns = CampaignRepository(session)
    metrics = MetricRepository(session)
    today = utc_today()

    campaign = await campaigns.create(
        name="Inverted funnel",
        platform=Platform.MOCK,
        daily_budget=200.0,
        total_budget=2000.0,
        target_cpa=50.0,
        target_roas=2.5,
        start_date=today - timedelta(days=10),
        status=CampaignStatus.ACTIVE,
    )
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today,
        impressions=1_000,
        cost=1234.567891,
    )
    await metrics.upsert_daily(
        campaign_id=campaign.id,
        stat_date=today,
        clicks=5_000,
        conversions=1_500,
        revenue=900.0,
    )
    return campaign.id


class TestMetricRepositorySnapshots:
    """The primary-datastore reader must honour the same slot contract.

    This path, not the warehouse, is what ``collect_run_inputs`` reads, so it is
    what every agent in a run actually reasons about.
    """

    async def test_both_granularities_are_not_summed_together(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        snapshots = await MetricRepository(session).snapshots(days=7)

        alpha = next(item for item in snapshots if item.campaign_id == portfolio["alpha"])
        # 10_000 + 8_000 from the two campaign-level slots. Adding today's two
        # creative rows would report 28_000 impressions and 1_400 of spend.
        assert alpha.impressions == 18_000
        assert alpha.clicks == 640
        assert alpha.conversions == 28
        assert alpha.total_cost == 900.0
        assert alpha.total_revenue == 2400.0

    async def test_it_agrees_with_the_warehouse_reader(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Two readers over one table must not disagree about the numbers."""
        repository = await MetricRepository(session).snapshots(days=7)
        warehouse = await SqlAggregateWarehouse(session).campaign_snapshots(None, days=7)

        def measures(items: list[Any]) -> dict[str, tuple[Any, ...]]:
            return {
                item.campaign_id: (
                    item.impressions,
                    item.clicks,
                    item.conversions,
                    item.total_cost,
                    item.total_revenue,
                )
                for item in items
            }

        assert measures(repository) == measures(warehouse)

    async def test_an_inverted_funnel_is_repaired_identically_by_both_readers(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """The agreement test has to feed data that makes the repair fire.

        With well-formed fixtures a clamp is a no-op on both sides, so the equality
        assertion passes even when only one reader performs it - which is how the
        two readers drifted apart without any test noticing.
        """
        campaign_id = await add_inverted_funnel_campaign(session)

        repository = await MetricRepository(session).snapshots(days=7)
        warehouse = await SqlAggregateWarehouse(session).campaign_snapshots(None, days=7)

        def measures(items: list[Any]) -> dict[str, tuple[Any, ...]]:
            return {
                item.campaign_id: (
                    item.impressions,
                    item.clicks,
                    item.conversions,
                    item.total_cost,
                    item.total_revenue,
                )
                for item in items
            }

        assert measures(repository) == measures(warehouse)

        repaired = next(item for item in repository if item.campaign_id == campaign_id)
        assert repaired.impressions == 1_000
        assert repaired.clicks == 1_000
        assert repaired.conversions == 1_000
        assert repaired.total_cost == 1234.5679
        assert repaired.total_revenue == 900.0

    async def test_a_breakdown_only_campaign_is_still_counted(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Having no roll-up row must not make a campaign read as zero and vanish."""
        campaign_id = await add_breakdown_only_campaign(session)

        snapshots = await MetricRepository(session).snapshots(days=7)

        only = next(item for item in snapshots if item.campaign_id == campaign_id)
        assert only.impressions == 5_000
        assert only.clicks == 140
        assert only.conversions == 9
        assert only.total_cost == 250.0
        assert only.total_revenue == 680.0

    async def test_the_window_still_bounds_the_sum(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        snapshots = await MetricRepository(session).snapshots([portfolio["alpha"]], days=1)

        assert len(snapshots) == 1
        assert snapshots[0].impressions == 10_000
        assert snapshots[0].total_cost == 500.0


class TestSqlAggregateWarehouse:
    async def test_snapshots_sum_only_the_requested_window(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        snapshots = await warehouse.campaign_snapshots(None, days=7)

        assert len(snapshots) == 2
        alpha = next(item for item in snapshots if item.campaign_id == portfolio["alpha"])
        assert alpha.campaign_name == "Alpha"
        assert alpha.impressions == 18_000
        assert alpha.clicks == 640
        assert alpha.conversions == 28
        assert alpha.total_cost == 900.0
        assert alpha.total_revenue == 2400.0
        assert alpha.ctr == pytest.approx(640 / 18_000)

    async def test_a_one_day_window_excludes_yesterday(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        snapshots = await warehouse.campaign_snapshots([portfolio["alpha"]], days=1)

        assert len(snapshots) == 1
        # 10_000 from the campaign-level slot alone. The two creative rows for the
        # same day break that total down, so adding them would report 20_000.
        assert snapshots[0].impressions == 10_000
        assert snapshots[0].conversions == 20
        assert snapshots[0].total_cost == 500.0

    async def test_the_id_filter_restricts_the_portfolio(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        snapshots = await warehouse.campaign_snapshots([portfolio["beta"]], days=7)

        assert [item.campaign_id for item in snapshots] == [portfolio["beta"]]

    async def test_creative_snapshots_split_delivery_by_creative(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        rows = await warehouse.creative_snapshots(portfolio["alpha"], days=7)

        by_id = {row["creative_id"]: row for row in rows}
        assert set(by_id) == {portfolio["hero"], portfolio["backup"]}
        assert by_id[portfolio["hero"]]["clicks"] == 300
        assert by_id[portfolio["hero"]]["revenue"] == 1100.0
        assert by_id[portfolio["backup"]]["conversions"] == 5

    async def test_creative_snapshots_ignore_rows_without_a_creative(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Campaign-level aggregates must not be mistaken for a creative."""
        warehouse = SqlAggregateWarehouse(session)

        rows = await warehouse.creative_snapshots(portfolio["beta"], days=7)

        assert rows == []

    async def test_audience_observations_fall_back_to_the_campaign_split(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Daily aggregates carry no demographics, and the code says so."""
        warehouse = SqlAggregateWarehouse(session)

        observations = await warehouse.audience_observations(None, days=7)

        assert {item.dimension for item in observations} == {"campaign"}
        assert {item.key for item in observations} == {"Alpha", "Beta"}
        alpha = next(item for item in observations if item.key == "Alpha")
        assert alpha.impressions == 18_000
        assert alpha.cost == 900.0

    async def test_timeseries_is_ordered_by_date_and_windowed(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        rows = await warehouse.timeseries(None, days=7)

        dates = [row["date"] for row in rows]
        assert dates == sorted(dates)
        assert len(dates) == 2
        newest = rows[-1]
        assert newest["date"] == utc_today().isoformat()
        assert newest["impressions"] == 12_000
        assert newest["cost"] == 700.0

    async def test_timeseries_can_be_scoped_to_one_campaign(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        warehouse = SqlAggregateWarehouse(session)

        rows = await warehouse.timeseries(portfolio["beta"], days=7)

        assert len(rows) == 1
        assert rows[0]["conversions"] == 1

    async def test_healthcheck_and_close_are_no_ops_on_sql(self, session: Any) -> None:
        warehouse = SqlAggregateWarehouse(session)

        assert await warehouse.healthcheck() == {"backend": "sql", "status": "ok"}
        assert await warehouse.close() is None
        assert warehouse.name == "sql"

    async def test_an_empty_database_yields_no_snapshots(self, session: Any) -> None:
        warehouse = SqlAggregateWarehouse(session)

        assert await warehouse.campaign_snapshots(None, days=7) == []
        assert await warehouse.timeseries(None, days=7) == []
        assert await warehouse.audience_observations(None, days=7) == []

    async def test_a_campaign_with_no_rollup_is_summed_from_its_breakdown(
        self, session: Any
    ) -> None:
        """An ingestion path that only stores creative rows must not read as zero."""
        campaign_id = await add_breakdown_only_campaign(session)
        warehouse = SqlAggregateWarehouse(session)

        snapshots = await warehouse.campaign_snapshots(None, days=7)

        assert [item.campaign_id for item in snapshots] == [campaign_id]
        assert snapshots[0].campaign_name == "Breakdown only"
        assert snapshots[0].impressions == 5_000
        assert snapshots[0].clicks == 140
        assert snapshots[0].conversions == 9
        assert snapshots[0].total_cost == 250.0
        assert snapshots[0].total_revenue == 680.0

    async def test_timeseries_falls_back_to_the_breakdown_per_day(self, session: Any) -> None:
        await add_breakdown_only_campaign(session)
        warehouse = SqlAggregateWarehouse(session)

        rows = await warehouse.timeseries(None, days=7)

        assert len(rows) == 2
        assert rows[-1]["impressions"] == 3_000
        assert rows[0]["revenue"] == 260.0

    async def test_a_rollup_and_a_breakdown_never_add_together(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Regression guard: this doubled every KPI the agents scored on."""
        await add_breakdown_only_campaign(session)
        warehouse = SqlAggregateWarehouse(session)

        snapshots = await warehouse.campaign_snapshots(None, days=7)

        by_id = {item.campaign_id: item for item in snapshots}
        # Alpha has both granularities, so its total comes from the roll-up only.
        assert by_id[portfolio["alpha"]].impressions == 18_000
        assert by_id[portfolio["alpha"]].total_cost == 900.0
        # The breakdown-only campaign is still present alongside it.
        assert len(snapshots) == 3
        rows = await warehouse.timeseries(None, days=7)
        assert rows[-1]["impressions"] == 12_000 + 3_000


class TestAnalyticsTrend:
    """/analytics/timeseries reads the same table the agents are scored from."""

    async def test_it_reports_each_day_once(self, session: Any, portfolio: dict[str, str]) -> None:
        rows = await AnalyticsService(session).timeseries(days=7)

        by_date = {row["date"]: row for row in rows}
        today = by_date[utc_today().isoformat()]
        # 10_000 + 2_000 from the two campaign-level slots. Adding today's two
        # creative rows would report 22_000 impressions and 1_200 of spend - a
        # trend line twice as tall as the delivery it describes.
        assert today["impressions"] == 12_000
        assert today["clicks"] == 440
        assert today["conversions"] == 21
        assert today["cost"] == 700.0
        assert today["revenue"] == 1800.0
        assert today["ctr"] == round(440 / 12_000, 6)
        assert today["roas"] == round(1800.0 / 700.0, 4)

    async def test_it_agrees_with_the_warehouse_trend(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        """Two readers over one table must not disagree about the numbers."""
        analytics = await AnalyticsService(session).timeseries(days=7)
        warehouse = await SqlAggregateWarehouse(session).timeseries(None, days=7)

        shared = ("date", "impressions", "clicks", "conversions", "cost", "revenue")
        assert [{key: row[key] for key in shared} for row in analytics] == [
            {key: row[key] for key in shared} for row in warehouse
        ]

    async def test_a_breakdown_only_campaign_still_reaches_the_trend(self, session: Any) -> None:
        """Having no roll-up row must not erase a campaign's delivery from the chart."""
        await add_breakdown_only_campaign(session)

        rows = await AnalyticsService(session).timeseries(days=7)

        assert len(rows) == 2
        assert rows[-1]["impressions"] == 3_000
        assert rows[0]["revenue"] == 260.0

    async def test_the_campaign_filter_still_applies(
        self, session: Any, portfolio: dict[str, str]
    ) -> None:
        rows = await AnalyticsService(session).timeseries(days=7, campaign_id=portfolio["beta"])

        assert len(rows) == 1
        assert rows[0]["conversions"] == 1
        assert rows[0]["cost"] == 200.0


class FakeResult:
    """The two attributes clickhouse_connect's query result exposes."""

    def __init__(self, column_names: list[str], result_rows: list[tuple[Any, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = result_rows


def route(sql: str) -> FakeResult:
    """Answer a query by its shape, the way a real warehouse would."""
    if "GROUP BY dimension, segment_key" in sql:
        return FakeResult(
            ["dimension", "segment_key", "impressions", "clicks", "conversions", "cost", "revenue"],
            [
                ("device", "mobile", 5000, 250, 20, 300.0, 900.0),
                ("country", "CN", 3000, 120, 9, 180.0, 520.0),
            ],
        )
    if "ORDER BY stat_date" in sql:
        return FakeResult(
            ["stat_date", "impressions", "clicks", "conversions", "cost", "revenue"],
            [
                (date(2026, 9, 6), 4000, 180, 12, 220.4567, 700.1234),
                (date(2026, 9, 7), 5000, 250, 20, 300.0, 900.0),
            ],
        )
    if ".campaigns" in sql:
        return FakeResult(["campaign_id", "campaign_name"], [("camp_a", "Alpha")])
    if "GROUP BY creative_id" in sql:
        return FakeResult(
            ["creative_id", "impressions", "clicks", "conversions", "cost", "revenue"],
            [("cre_1", 3000, 150, 12, 180.0, 600.0)],
        )
    return FakeResult(
        ["campaign_id", "impressions", "clicks", "conversions", "cost", "revenue"],
        [("camp_a", 9000, 400, 32, 480.0, 1500.0)],
    )


class FakeDriver:
    """Mimics the clickhouse_connect client closely enough for the read model."""

    def __init__(self, *, fail_query: bool = False, fail_command: bool = False) -> None:
        self.fail_query = fail_query
        self.fail_command = fail_command
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[str] = []
        self.closed = False

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> FakeResult:
        self.queries.append((sql, dict(parameters or {})))
        if self.fail_query:
            raise RuntimeError("clickhouse is on fire")
        return route(sql)

    def command(self, sql: str) -> str:
        self.commands.append(sql)
        if self.fail_command:
            raise RuntimeError("clickhouse is on fire")
        return "1"

    def close(self) -> None:
        self.closed = True

    @property
    def last_sql(self) -> str:
        return self.queries[-1][0]

    @property
    def last_parameters(self) -> dict[str, Any]:
        return self.queries[-1][1]

    @property
    def first_sql(self) -> str:
        """campaign_snapshots issues a second query for names; this is the data one."""
        return self.queries[0][0]

    @property
    def first_parameters(self) -> dict[str, Any]:
        return self.queries[0][1]


def connected(
    *, fail_query: bool = False, fail_command: bool = False
) -> tuple[ClickHouseWarehouse, FakeDriver]:
    """A warehouse on the events source, with the driver already wired up.

    The source is pinned rather than inherited: everything in this class asserts
    the shape of event-stream reads, and ``daily`` is now the configured default.
    """
    settings = ClickHouseSettings(enabled=True, database="ad_optimizer", metrics_source="events")
    warehouse = ClickHouseWarehouse(settings)
    driver = FakeDriver(fail_query=fail_query, fail_command=fail_command)
    warehouse._client = driver
    return warehouse, driver


class TestClickHouseWarehouse:
    async def test_campaign_snapshots_resolve_names_in_a_second_query(self) -> None:
        warehouse, driver = connected()

        snapshots = await warehouse.campaign_snapshots(None, days=7)

        assert len(snapshots) == 1
        assert snapshots[0].campaign_id == "camp_a"
        assert snapshots[0].campaign_name == "Alpha"
        assert snapshots[0].impressions == 9000
        assert snapshots[0].total_revenue == 1500.0
        assert len(driver.queries) == 2
        assert "GROUP BY campaign_id" in driver.queries[0][0]

    async def test_the_window_is_a_bound_placeholder_never_interpolated(self) -> None:
        """A day count that reached the SQL text would be an injection vector."""
        warehouse, driver = connected()

        await warehouse.campaign_snapshots(None, days=14)

        assert "{days:UInt16}" in driver.first_sql
        assert "{db:Identifier}" in driver.first_sql
        assert driver.first_parameters == {"db": "ad_optimizer", "days": 14}
        assert "14" not in driver.first_sql

    async def test_a_campaign_filter_binds_an_array_parameter(self) -> None:
        warehouse, driver = connected()

        await warehouse.campaign_snapshots(["camp_a", "camp_b"], days=7)

        assert "{ids:Array(String)}" in driver.first_sql
        assert driver.first_parameters["ids"] == ["camp_a", "camp_b"]

    async def test_an_empty_filter_list_binds_nothing(self) -> None:
        warehouse, driver = connected()

        await warehouse.campaign_snapshots([], days=7)

        assert "ids" not in driver.first_parameters
        assert "{ids:Array(String)}" not in driver.first_sql

    async def test_creative_snapshots_bind_the_campaign(self) -> None:
        warehouse, driver = connected()

        rows = await warehouse.creative_snapshots("camp_a", days=3)

        assert rows == [
            {
                "creative_id": "cre_1",
                "impressions": 3000,
                "clicks": 150,
                "conversions": 12,
                "cost": 180.0,
                "revenue": 600.0,
            }
        ]
        assert driver.last_parameters["campaign_id"] == "camp_a"

    async def test_audience_observations_union_every_demographic_dimension(self) -> None:
        warehouse, driver = connected()

        observations = await warehouse.audience_observations(None, days=7)

        assert {item.dimension for item in observations} == {"device", "country"}
        assert {item.key for item in observations} == {"mobile", "CN"}
        mobile = next(item for item in observations if item.key == "mobile")
        assert mobile.conversions == 20
        assert mobile.revenue == 900.0
        for dimension in ClickHouseWarehouse.DIMENSIONS:
            assert "'" + dimension + "' AS dimension" in driver.last_sql
        assert driver.last_sql.count("UNION ALL") == len(ClickHouseWarehouse.DIMENSIONS) - 1

    async def test_audience_observations_accept_a_campaign_filter(self) -> None:
        warehouse, driver = connected()

        await warehouse.audience_observations(["camp_a"], days=7)

        assert "{ids:Array(String)}" in driver.last_sql
        assert driver.last_parameters["ids"] == ["camp_a"]

    async def test_timeseries_rounds_money_and_binds_the_campaign(self) -> None:
        warehouse, driver = connected()

        rows = await warehouse.timeseries("camp_a", days=7)

        assert [row["date"] for row in rows] == ["2026-09-06", "2026-09-07"]
        assert rows[0]["cost"] == 220.46
        assert rows[0]["revenue"] == 700.12
        assert driver.last_parameters["campaign_id"] == "camp_a"

    async def test_timeseries_without_a_campaign_omits_the_parameter(self) -> None:
        warehouse, driver = connected()

        rows = await warehouse.timeseries(None, days=7)

        assert len(rows) == 2
        assert "campaign_id" not in driver.last_parameters
        assert "{campaign_id:String}" not in driver.last_sql

    async def test_a_driver_failure_degrades_to_an_empty_result(self) -> None:
        """A warehouse outage must not take the optimization run down with it."""
        warehouse, driver = connected(fail_query=True)

        assert await warehouse.campaign_snapshots(None, days=7) == []
        assert await warehouse.creative_snapshots("camp_a", days=7) == []
        assert await warehouse.audience_observations(None, days=7) == []
        assert await warehouse.timeseries(None, days=7) == []
        assert driver.queries

    async def test_queries_are_skipped_before_a_connection_exists(self) -> None:
        warehouse = ClickHouseWarehouse(ClickHouseSettings(enabled=True))

        assert await warehouse.campaign_snapshots(None, days=7) == []
        assert await warehouse.creative_snapshots("camp_a", days=7) == []
        assert await warehouse.timeseries(None, days=7) == []
        assert await warehouse.audience_observations(None, days=7) == []

    async def test_healthcheck_reports_ok_error_and_unavailable(self) -> None:
        healthy, healthy_driver = connected()
        broken, _ = connected(fail_command=True)
        never_connected = ClickHouseWarehouse(ClickHouseSettings(enabled=True))

        assert await healthy.healthcheck() == {"backend": "clickhouse", "status": "ok"}
        assert healthy_driver.commands == ["SELECT 1"]

        failure = await broken.healthcheck()
        assert failure["status"] == "error"
        assert "on fire" in str(failure["error"])

        assert await never_connected.healthcheck() == {
            "backend": "clickhouse",
            "status": "unavailable",
        }

    async def test_close_releases_the_driver_and_is_idempotent(self) -> None:
        warehouse, driver = connected()

        await warehouse.close()
        assert driver.closed is True

        await warehouse.close()
        assert await warehouse.healthcheck() == {
            "backend": "clickhouse",
            "status": "unavailable",
        }

    async def test_connect_reports_failure_when_the_driver_is_not_installed(self) -> None:
        """The clickhouse extra is optional; a missing driver is not an error."""
        warehouse = ClickHouseWarehouse(ClickHouseSettings(enabled=True))

        assert await warehouse.connect() is False
        assert warehouse._client is None


class RecordingLogger:
    """Records structured log calls instead of emitting them.

    ``structlog.testing.capture_logs`` is the obvious tool and is not a safe one
    here, for the reason ``test_llm_gateway`` documents: ``configure_logging``
    installs a fresh processor list on every app-factory run while loggers are
    cached on first use, so a capture can come back silently empty under a
    full-suite run and turn a "did not warn" assertion into a vacuous pass.
    Swapping the module attribute has no such coupling.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def debug(self, event: str, **fields: Any) -> None:
        self.events.append(("debug", event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append(("warning", event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self.events.append(("error", event, fields))

    def matching(self, level: str, event: str) -> list[dict[str, Any]]:
        return [
            fields
            for seen_level, seen_event, fields in self.events
            if seen_level == level and seen_event == event
        ]


class TestWarehouseSelection:
    async def test_sql_is_used_when_clickhouse_is_disabled(
        self, session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///:memory:", clickhouse=ClickHouseSettings(enabled=False)
        )
        monkeypatch.setattr(warehouse_module, "get_settings", lambda: settings)

        warehouse = await build_warehouse(session)

        assert isinstance(warehouse, SqlAggregateWarehouse)

    async def test_an_unreachable_warehouse_falls_back_to_sql(
        self, session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Misconfigured analytics must never block the optimization loop."""
        settings = make_settings(
            "sqlite+aiosqlite:///:memory:", clickhouse=ClickHouseSettings(enabled=True)
        )
        monkeypatch.setattr(warehouse_module, "get_settings", lambda: settings)

        warehouse = await build_warehouse(session)

        assert isinstance(warehouse, SqlAggregateWarehouse)

    async def test_a_reachable_warehouse_is_preferred(
        self, session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = make_settings(
            "sqlite+aiosqlite:///:memory:", clickhouse=ClickHouseSettings(enabled=True)
        )
        monkeypatch.setattr(warehouse_module, "get_settings", lambda: settings)

        async def always_connects(self: ClickHouseWarehouse) -> bool:
            self._client = FakeDriver()
            return True

        monkeypatch.setattr(ClickHouseWarehouse, "connect", always_connects)

        warehouse = await build_warehouse(session)

        assert isinstance(warehouse, ClickHouseWarehouse)
        assert warehouse.name == "clickhouse"
        await warehouse.close()

    async def test_an_events_source_says_that_nothing_writes_it(
        self, session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The richer source has no writer yet, so wiring it up must not be silent.

        Reading ``ad_events`` answers empty, and an empty answer falls back to the
        primary datastore by design - which is precisely the quiet failure an
        operator cannot distinguish from "no delivery in this window".
        """
        settings = make_settings(
            "sqlite+aiosqlite:///:memory:",
            clickhouse=ClickHouseSettings(enabled=True, metrics_source="events"),
        )
        monkeypatch.setattr(warehouse_module, "get_settings", lambda: settings)
        recorder = RecordingLogger()
        monkeypatch.setattr(warehouse_module, "logger", recorder)

        async def always_connects(self: ClickHouseWarehouse) -> bool:
            self._client = FakeDriver()
            return True

        monkeypatch.setattr(ClickHouseWarehouse, "connect", always_connects)

        warehouse = await build_warehouse(session)

        assert isinstance(warehouse, ClickHouseWarehouse)
        warned = recorder.matching("warning", "clickhouse_events_source_has_no_writer")
        assert len(warned) == 1
        assert warned[0]["table"] == "ad_events"
        await warehouse.close()

    async def test_the_default_source_is_not_news(
        self, session: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``daily`` has a writer, so a warning here would only train people to ignore it."""
        settings = make_settings(
            "sqlite+aiosqlite:///:memory:", clickhouse=ClickHouseSettings(enabled=True)
        )
        monkeypatch.setattr(warehouse_module, "get_settings", lambda: settings)
        recorder = RecordingLogger()
        monkeypatch.setattr(warehouse_module, "logger", recorder)

        async def always_connects(self: ClickHouseWarehouse) -> bool:
            self._client = FakeDriver()
            return True

        monkeypatch.setattr(ClickHouseWarehouse, "connect", always_connects)

        warehouse = await build_warehouse(session)

        assert isinstance(warehouse, ClickHouseWarehouse)
        assert recorder.matching("warning", "clickhouse_events_source_has_no_writer") == []
        await warehouse.close()


def daily_connected(*, fail_query: bool = False) -> tuple[ClickHouseWarehouse, FakeDriver]:
    """A warehouse reading the aggregate table, with the driver already wired."""
    settings = ClickHouseSettings(enabled=True, database="ad_optimizer", metrics_source="daily")
    warehouse = ClickHouseWarehouse(settings)
    driver = FakeDriver(fail_query=fail_query)
    warehouse._client = driver
    return warehouse, driver


class TestClickHouseDailySource:
    """The aggregate-table source, which is the one ingestion can populate today."""

    async def test_the_source_with_a_writer_is_the_default(self) -> None:
        """Defaulting to a table nothing writes would read empty and hide it.

        ``events`` looks like the safe default - "do not change what an existing
        deployment reads" - but no production code path writes ``ad_events``, so
        all it preserves is an empty result plus the quiet fallback to the
        primary datastore that follows it.
        """
        warehouse = ClickHouseWarehouse(ClickHouseSettings(enabled=True))

        assert warehouse.source == "daily"
        assert warehouse._table == "campaign_daily_metrics"

    async def test_the_events_source_stays_reachable_as_an_opt_in(self) -> None:
        """The richer shape must not be lost, only stopped from being the default."""
        settings = ClickHouseSettings(enabled=True, metrics_source="events")

        warehouse = ClickHouseWarehouse(settings)

        assert warehouse.source == "events"
        assert warehouse._table == "ad_events"

    async def test_the_aggregate_table_is_summed_not_counted(self) -> None:
        """Counting rows in a table that already holds totals undercounts by orders."""
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=7)

        assert "campaign_daily_metrics" in driver.first_sql
        assert "sum(impressions)" in driver.first_sql
        # ``countIf`` does appear here, but on creative_id - the granularity probe
        # the slot collapse needs. What must not appear is the event-type counting
        # that only means anything over ad_events.
        assert "countIf(event_type" not in driver.first_sql
        assert "ad_events" not in driver.first_sql

    async def test_a_rollup_bucket_is_not_summed_with_its_breakdown(self) -> None:
        """The mirror stores both granularities, so a bare sum doubles every KPI.

        ``export_daily`` copies the campaign roll-up and the creative breakdown as
        separate rows, exactly as the primary datastore holds them. This is the
        warehouse-side half of the double-count the two SQL readers already guard.
        """
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=7)

        sql = driver.first_sql
        assert "GROUP BY campaign_id, stat_date" in sql
        assert "countIf(creative_id = '') > 0" in sql
        for measure in MEASURES:
            assert "sumIf(" + measure + ", creative_id = '')" in sql
            assert "sumIf(" + measure + ", creative_id != '')" in sql

    async def test_the_rollup_preference_is_decided_per_bucket(self) -> None:
        """One campaign must not decide the granularity for another.

        Collapsing per campaign instead of per (campaign, day) would read a
        campaign that only ever stored creative rows as zero and drop it out of the
        report entirely - the failure the SQL-side slot merge exists to prevent.
        """
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=7)

        sql = driver.first_sql
        assert sql.endswith("GROUP BY campaign_id, stat_date) GROUP BY campaign_id")

    async def test_timeseries_collapses_the_granularities_per_day(self) -> None:
        warehouse, driver = daily_connected()

        await warehouse.timeseries(None, days=7)

        sql = driver.last_sql
        assert "sumIf(cost, creative_id = '')" in sql
        assert sql.endswith(
            "GROUP BY campaign_id, stat_date) GROUP BY stat_date ORDER BY stat_date"
        )

    async def test_creative_rows_keep_the_breakdown_granularity(self) -> None:
        """Per-creative delivery *is* the breakdown; collapsing it would erase it."""
        warehouse, driver = daily_connected()

        await warehouse.creative_snapshots("camp_a", days=7)

        assert "GROUP BY creative_id" in driver.last_sql
        assert "GROUP BY campaign_id, stat_date" not in driver.last_sql

    async def test_a_replayed_window_is_deduped_at_read_time(self) -> None:
        """Sync is re-runnable, so the read must collapse the versions it leaves.

        ``campaign_daily_metrics`` is a ``ReplacingMergeTree``: replaying the same
        window inserts a second version of every row, and ClickHouse only drops
        the superseded one during a background merge it schedules itself. Summing
        before that merge adds the same delivery twice - impressions, spend and
        revenue all double while ROAS, a ratio of two equally inflated numbers,
        still looks sane, so nothing downstream complains. ``FINAL`` is what makes
        the idempotence ``warehouse sync`` promises hold for readers and not only
        for the table. The placement is asserted too: ``FROM t WHERE ... FINAL``
        is a syntax error, so a well-intentioned edit can break this silently.
        """
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=7)
        await warehouse.timeseries(None, days=7)
        await warehouse.creative_snapshots("camp_a", days=7)

        reads = [sql for sql, _parameters in driver.queries if "campaign_daily_metrics" in sql]
        # campaign_snapshots also issues a name lookup against ``campaigns``, so
        # counting every query would assert against the wrong relation.
        assert len(reads) == 3
        for sql in reads:
            assert "campaign_daily_metrics FINAL WHERE" in sql

    async def test_the_events_source_pays_no_dedup(self) -> None:
        """``ad_events`` is a plain MergeTree: one row is one delivery, no versions."""
        warehouse, driver = connected()

        await warehouse.campaign_snapshots(None, days=7)
        await warehouse.timeseries(None, days=7)
        await warehouse.creative_snapshots("camp_a", days=7)

        for sql, _parameters in driver.queries:
            assert "FINAL" not in sql

    async def test_the_events_source_reads_the_bare_table(self) -> None:
        """One event is one delivery at one granularity, so no collapse is needed."""
        warehouse, driver = connected()

        await warehouse.campaign_snapshots(None, days=7)
        await warehouse.timeseries(None, days=7)

        for sql, _parameters in driver.queries:
            # One event belongs to exactly one creative, so there is no second
            # granularity to keep apart and the collapse must stay out of the way.
            assert "creative_id" not in sql
            assert "GROUP BY campaign_id, stat_date" not in sql
        assert "FROM {db:Identifier}.ad_events WHERE" in driver.first_sql
        assert "FROM {db:Identifier}.ad_events WHERE" in driver.last_sql

    async def test_the_window_filters_on_stat_date(self) -> None:
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=14)

        assert "stat_date >= today()" in driver.first_sql
        assert "event_time" not in driver.first_sql
        assert driver.first_parameters == {"db": "ad_optimizer", "days": 14}

    async def test_the_daily_window_counts_today_as_its_last_day(self) -> None:
        """``days`` counts calendar days *including* today, not days before today.

        Every SQL reader uses ``today - (days - 1)``. Without the subtraction this
        reader answers a one-day request with two days of delivery, and the two
        backends disagree about the same endpoint with nothing to show which is
        right.
        """
        warehouse, driver = daily_connected()

        await warehouse.campaign_snapshots(None, days=1)

        assert "toIntervalDay(greatest({days:UInt16}, 1) - 1)" in driver.first_sql
        assert driver.first_parameters["days"] == 1

    async def test_a_zero_day_window_cannot_wrap_into_the_whole_table(self) -> None:
        """The placeholder binds as UInt16, where ``0 - 1`` is 65535 days."""
        warehouse, _driver = daily_connected()

        assert "greatest({days:UInt16}, 1)" in warehouse._window_filter(0)

    async def test_creative_rows_exclude_the_campaign_rollup(self) -> None:
        """The roll-up is stored with an empty creative_id and must not be reported as one."""
        warehouse, driver = daily_connected()

        await warehouse.creative_snapshots("camp_a", days=7)

        assert "creative_id != ''" in driver.last_sql
        assert driver.last_parameters["campaign_id"] == "camp_a"

    async def test_the_events_source_does_not_filter_on_creative_id(self) -> None:
        """An event always belongs to a creative, so the guard would be noise."""
        warehouse, driver = connected()

        await warehouse.creative_snapshots("camp_a", days=7)

        assert "creative_id != ''" not in driver.last_sql

    async def test_audience_falls_back_to_the_campaign_split(self) -> None:
        """A daily report carries no demographics; inventing them would be worse."""
        warehouse, driver = daily_connected()

        observations = await warehouse.audience_observations(None, days=7)

        assert {item.dimension for item in observations} == {"campaign"}
        assert all("UNION ALL" not in sql for sql, _parameters in driver.queries)

    async def test_timeseries_groups_on_the_stored_date(self) -> None:
        warehouse, driver = daily_connected()

        await warehouse.timeseries(None, days=7)

        assert "SELECT stat_date AS stat_date" in driver.last_sql
        assert "toDate(event_time)" not in driver.last_sql
        assert "ORDER BY stat_date" in driver.last_sql

    async def test_the_date_expression_switches_with_the_source(self) -> None:
        events_warehouse, _ = connected()
        daily_warehouse, _ = daily_connected()

        assert events_warehouse._date_expression() == "toDate(event_time)"
        assert daily_warehouse._date_expression() == "stat_date"
