"""Metric feeds, the wire contract, and the rules that decide what lands.

Everything here is exercised without a database, because these are the parts that
have to be right before a row is allowed anywhere near one: a feed that is not
deterministic cannot be reasoned about, and a validation rule that is wrong
discards real data without telling anybody.

The same code is driven end to end over HTTP in
``tests/integration/test_ingest_api.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from adoptimizer.core.clock import utc_today
from adoptimizer.core.config import DataMode
from adoptimizer.core.errors import ExternalServiceError, NotFoundError
from adoptimizer.domain.enums import Platform
from adoptimizer.domain.kpi import DAILY_MEASUREMENTS
from adoptimizer.infra.ads.mock import MockAdsClient
from adoptimizer.infra.ads.registry import PlatformRegistry, build_platform_clients
from adoptimizer.infra.ingest import (
    SYNTHETIC_SEED,
    MetricSourceRegistry,
    PlatformReportSource,
    SourceRecord,
    SourceTarget,
    SyntheticMetricSource,
    build_metric_sources,
)
from adoptimizer.schemas.ingest import (
    MAX_BATCH_RECORDS,
    IngestBatchIn,
    IngestIssue,
    IngestReportOut,
    MetricRecordIn,
)
from adoptimizer.services.ingest import (
    MAX_FUTURE_DAYS,
    _Attributed,
    _reject_duplicate_slots,
    _resolve_campaign,
    _semantic_problem,
    to_metric_input,
)

WINDOW_START = date(2026, 9, 1)
WINDOW_END = date(2026, 9, 3)
TARGET = SourceTarget(platform=Platform.GOOGLE, external_id="ext_google_1000", campaign_id="camp_a")


def record(**overrides: Any) -> MetricRecordIn:
    """A well-formed record, so a test can break exactly one thing."""
    payload: dict[str, Any] = {
        "campaign_id": "camp_a",
        "stat_date": utc_today(),
        "impressions": 1000,
        "clicks": 40,
        "conversions": 3,
        "cost": 51.25,
        "revenue": 210.0,
    }
    payload.update(overrides)
    return MetricRecordIn.model_validate(payload)


class ReportingMockClient(MockAdsClient):
    """A platform adapter that answers ``fetch_report`` with fixed rows."""

    def __init__(self, platform: Platform, rows: list[dict[str, Any]]) -> None:
        super().__init__(platform)
        self._rows = rows

    async def fetch_report(
        self, external_id: str, *, start_date: str, end_date: str
    ) -> dict[str, Any]:
        return {
            "campaign_id": external_id,
            "start_date": start_date,
            "end_date": end_date,
            "source": "test",
            "rows": self._rows,
        }


class TestSyntheticSource:
    """The demo feed has to be reproducible or nothing built on it can be tested."""

    async def test_the_same_window_replays_identically(self) -> None:
        source = SyntheticMetricSource()

        first = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])
        second = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert first == second
        assert len(first) == 3

    async def test_a_different_campaign_gets_different_numbers(self) -> None:
        source = SyntheticMetricSource()
        other = SourceTarget(Platform.META, "ext_meta_1001", "camp_b")

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_START, targets=[TARGET, other])

        assert rows[0].impressions != rows[1].impressions

    async def test_the_numbers_are_internally_consistent(self) -> None:
        """A feed whose clicks exceed its impressions would break every ratio downstream."""
        source = SyntheticMetricSource()

        rows = await source.fetch(
            start=utc_today() - timedelta(days=30), end=utc_today(), targets=[TARGET]
        )

        assert len(rows) == 31
        for row in rows:
            assert row.impressions is not None and row.impressions > 0
            assert row.clicks is not None and 0 < row.clicks <= row.impressions
            assert row.conversions is not None and row.conversions <= row.clicks
            assert row.cost is not None and row.cost > 0
            assert row.revenue is not None and row.revenue > 0
            assert row.unique_reach is not None and 0 < row.unique_reach <= row.impressions

    async def test_narrowing_the_columns_does_not_shift_the_others(self) -> None:
        """Dropping revenue must not change impressions.

        Both instances draw from the same seeded stream, so if a column were
        skipped instead of discarded the remaining values would move and two
        configurations of the same feed would disagree about history.
        """
        full = await SyntheticMetricSource().fetch(
            start=WINDOW_START, end=WINDOW_END, targets=[TARGET]
        )
        partial = await SyntheticMetricSource(columns=("impressions", "cost")).fetch(
            start=WINDOW_START, end=WINDOW_END, targets=[TARGET]
        )

        assert [(row.impressions, row.cost) for row in full] == [
            (row.impressions, row.cost) for row in partial
        ]
        assert all(row.revenue is None for row in partial)
        assert all(row.clicks is None for row in partial)

    def test_an_unknown_column_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown measurement columns"):
            SyntheticMetricSource(columns=("impressions", "spend"))

    async def test_no_targets_means_nothing_to_say(self) -> None:
        """It cannot enumerate campaigns, so it must not invent any."""
        assert await SyntheticMetricSource().fetch(start=WINDOW_START, end=WINDOW_END) == []

    async def test_an_inverted_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="end precedes start"):
            await SyntheticMetricSource().fetch(
                start=WINDOW_END, end=WINDOW_START, targets=[TARGET]
            )

    def test_it_needs_no_credentials(self) -> None:
        assert SyntheticMetricSource().is_configured is True

    def test_the_seed_is_a_published_constant(self) -> None:
        """A test asserting an exact number has to be able to pin the seed."""
        assert SyntheticMetricSource().columns == frozenset(DAILY_MEASUREMENTS)
        assert SYNTHETIC_SEED > 0


class TestPlatformReportSource:
    """The real socket: rows from a platform adapter, converted not invented."""

    def registry(self, mode: DataMode, rows: list[dict[str, Any]]) -> PlatformRegistry:
        return PlatformRegistry(
            {Platform.GOOGLE: ReportingMockClient(Platform.GOOGLE, rows)}, data_mode=mode
        )

    async def test_rows_become_records(self) -> None:
        source = PlatformReportSource(
            self.registry(
                DataMode.WAREHOUSE,
                [
                    {
                        "date": "2026-09-01",
                        "impressions": 500,
                        "clicks": 12,
                        "conversions": 1,
                        "cost": 9.5,
                    }
                ],
            )
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert len(rows) == 1
        assert rows[0].stat_date == date(2026, 9, 1)
        assert rows[0].impressions == 500
        assert rows[0].cost == 9.5
        assert rows[0].external_id == "ext_google_1000"
        assert rows[0].campaign_id == "camp_a"

    async def test_revenue_is_never_asserted_by_a_platform(self) -> None:
        """No ad network knows your revenue; claiming zero would erase the truth."""
        source = PlatformReportSource(
            self.registry(
                DataMode.WAREHOUSE, [{"date": "2026-09-01", "impressions": 1, "cost": 1.0}]
            )
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_START, targets=[TARGET])

        assert rows[0].revenue is None
        assert rows[0].unique_reach is None
        assert rows[0].clicks is None

    async def test_a_timestamp_date_is_parsed(self) -> None:
        """TikTok returns "2026-09-01 00:00:00", which fromisoformat rejects whole."""
        source = PlatformReportSource(
            self.registry(DataMode.WAREHOUSE, [{"date": "2026-09-02 00:00:00", "impressions": 7}])
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert [row.stat_date for row in rows] == [date(2026, 9, 2)]

    async def test_a_row_with_no_usable_date_is_dropped_not_dated_by_guess(self) -> None:
        source = PlatformReportSource(
            self.registry(
                DataMode.WAREHOUSE,
                [{"date": None, "impressions": 7}, {"impressions": 8}, "not-a-row"],
            )
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert rows == []

    async def test_an_already_parsed_date_is_passed_through(self) -> None:
        """A warehouse driver can hand back real date objects; re-parsing is a guess."""
        source = PlatformReportSource(
            self.registry(DataMode.WAREHOUSE, [{"date": date(2026, 9, 2), "impressions": 3}])
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert [row.stat_date for row in rows] == [date(2026, 9, 2)]

    async def test_a_date_shaped_like_a_date_but_not_one_is_dropped(self) -> None:
        """Ten characters that are not a day: fail closed rather than invent one."""
        source = PlatformReportSource(
            self.registry(DataMode.WAREHOUSE, [{"date": "2026-13-45", "impressions": 3}])
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert rows == []

    async def test_a_suppressed_column_is_absent_not_zero(self) -> None:
        """Networks render a withheld metric as "-" or "n/a".

        Reading either as zero would understate spend, and understated spend is
        the one error the optimizer cannot detect from the numbers alone.
        """
        source = PlatformReportSource(
            self.registry(
                DataMode.WAREHOUSE,
                [{"date": "2026-09-01", "impressions": "-", "cost": "n/a", "clicks": "12"}],
            )
        )

        rows = await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

        assert rows[0].impressions is None
        assert rows[0].cost is None
        assert rows[0].clicks == 12

    def test_mock_mode_reports_itself_unconfigured(self) -> None:
        """The mock adapter has no rows, so a green pull would mean nothing."""
        assert PlatformReportSource(self.registry(DataMode.MOCK, [])).is_configured is False

    async def test_mock_mode_raises_instead_of_returning_nothing(self) -> None:
        source = PlatformReportSource(self.registry(DataMode.MOCK, []))

        with pytest.raises(ExternalServiceError, match="DATA_MODE=warehouse"):
            await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

    def test_an_uncredentialed_adapter_is_not_configured(self) -> None:
        client = MockAdsClient(Platform.GOOGLE)
        client.is_configured = False
        registry = PlatformRegistry({Platform.GOOGLE: client}, data_mode=DataMode.WAREHOUSE)

        assert PlatformReportSource(registry).is_configured is False

    async def test_an_uncredentialed_adapter_raises_out_of_the_registry(self) -> None:
        client = MockAdsClient(Platform.GOOGLE)
        client.is_configured = False
        source = PlatformReportSource(
            PlatformRegistry({Platform.GOOGLE: client}, data_mode=DataMode.WAREHOUSE)
        )

        with pytest.raises(ExternalServiceError):
            await source.fetch(start=WINDOW_START, end=WINDOW_END, targets=[TARGET])

    async def test_an_inverted_window_is_refused(self) -> None:
        source = PlatformReportSource(self.registry(DataMode.WAREHOUSE, []))

        with pytest.raises(ValueError, match="end precedes start"):
            await source.fetch(start=WINDOW_END, end=WINDOW_START, targets=[TARGET])


class TestSourceRecord:
    def test_measurements_lists_only_what_was_asserted(self) -> None:
        row = SourceRecord(stat_date=WINDOW_START, impressions=10, cost=1.5)

        assert row.measurements() == {"impressions": 10, "cost": 1.5}

    def test_a_zero_measurement_is_still_a_measurement(self) -> None:
        """Zero means "nothing happened"; absent means "not measured"."""
        assert SourceRecord(stat_date=WINDOW_START, conversions=0).measurements() == {
            "conversions": 0
        }

    def test_identity_describes_whichever_scheme_was_used(self) -> None:
        assert (
            SourceRecord(stat_date=WINDOW_START, campaign_id="camp_a").identity()
            == "campaign_id=camp_a"
        )
        assert (
            SourceRecord(
                stat_date=WINDOW_START, platform=Platform.META, external_id="e1"
            ).identity()
            == "meta/e1"
        )
        assert SourceRecord(stat_date=WINDOW_START).identity() == "unaddressed"


class TestSourceRegistry:
    def test_both_feeds_are_registered(self) -> None:
        registry = build_metric_sources(build_platform_clients(DataMode.MOCK))

        assert registry.names() == ["platform", "synthetic"]
        assert len(registry) == 2
        assert "synthetic" in registry

    def test_only_the_usable_feeds_are_reported_configured(self) -> None:
        registry = build_metric_sources(build_platform_clients(DataMode.MOCK))

        assert registry.configured() == ["synthetic"]
        assert registry.status() == {
            "platform": {"registered": True, "configured": False},
            "synthetic": {"registered": True, "configured": True},
        }

    def test_an_unknown_feed_is_not_found_and_the_alternatives_are_named(self) -> None:
        registry = build_metric_sources(build_platform_clients(DataMode.MOCK))

        with pytest.raises(NotFoundError) as caught:
            registry.get("google")

        assert "platform, synthetic" in str(caught.value)

    def test_a_duplicate_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Duplicate metric source name"):
            MetricSourceRegistry([SyntheticMetricSource(), SyntheticMetricSource()])

    def test_an_empty_registry_says_so(self) -> None:
        with pytest.raises(NotFoundError, match="Available: none"):
            MetricSourceRegistry([]).get("synthetic")


class TestRecordContract:
    def test_a_blank_id_is_treated_as_absent(self) -> None:
        """CSV-shaped producers send "" for every column they do not populate."""
        parsed = MetricRecordIn.model_validate(
            {
                "campaign_id": "  ",
                "external_id": "",
                "platform": "google",
                "stat_date": "2026-09-01",
                "impressions": 5,
            }
        )

        assert parsed.campaign_id is None
        assert parsed.external_id is None
        # A platform with no external id is not an address.
        assert parsed.identity() == "unaddressed"

    def test_a_fractional_count_is_rounded_rather_than_rejecting_the_batch(self) -> None:
        assert record(conversions=2.6).conversions == 3
        assert record(impressions=1000.0).impressions == 1000

    def test_a_negative_value_survives_parsing_to_be_reported_per_row(self) -> None:
        """Rejecting it here would take 4,999 good rows down with one bad one."""
        assert record(cost=-4.0).cost == -4.0

    def test_the_source_name_is_normalised_before_the_pattern_is_applied(self) -> None:
        assert (
            IngestBatchIn(source=" Warehouse.Export ", records=[record()]).source
            == "warehouse.export"
        )

    def test_a_source_name_outside_the_allowed_shape_is_refused(self) -> None:
        with pytest.raises(ValueError):
            IngestBatchIn(source="Not A Label", records=[record()])

    def test_an_unknown_field_is_refused(self) -> None:
        """A renamed column must fail loudly, not be read as zero."""
        with pytest.raises(ValueError):
            MetricRecordIn.model_validate(
                {"campaign_id": "c", "stat_date": "2026-09-01", "impressions": 1, "spend": 2.0}
            )

    def test_a_batch_is_bounded(self) -> None:
        with pytest.raises(ValueError):
            IngestBatchIn.model_validate({"source": "s", "records": []})
        assert MAX_BATCH_RECORDS > 0

    def test_measurements_present_lists_only_the_asserted_columns(self) -> None:
        assert record().measurements_present() == (
            "impressions",
            "clicks",
            "conversions",
            "cost",
            "revenue",
        )
        assert MetricRecordIn(campaign_id="c", stat_date=WINDOW_START).measurements_present() == ()


class TestReportAccounting:
    def test_a_report_that_does_not_add_up_is_refused(self) -> None:
        report = IngestReportOut(source="s", received=10, created=3, updated=4)

        with pytest.raises(AssertionError, match="does not reconcile"):
            report.reconciles()

    def test_a_report_that_adds_up_passes(self) -> None:
        report = IngestReportOut(
            source="s", received=10, created=3, updated=4, rejected_count=2, unresolved_count=1
        )

        report.reconciles()
        assert report.accepted == 7

    def test_a_dry_run_still_has_to_add_up(self) -> None:
        report = IngestReportOut(source="s", dry_run=True, received=2, created=2)

        report.reconciles()


class TestSemanticRules:
    def test_a_well_formed_record_passes(self) -> None:
        assert _semantic_problem(record()) is None

    def test_a_record_with_no_identity_is_rejected(self) -> None:
        problem = _semantic_problem(record(campaign_id=None))

        assert problem is not None and "cannot be attributed" in problem

    def test_a_negative_measurement_is_rejected_and_named(self) -> None:
        problem = _semantic_problem(record(cost=-1.0, conversions=-2))

        assert problem is not None and "conversions, cost" in problem

    def test_a_record_that_measures_nothing_is_rejected(self) -> None:
        empty = MetricRecordIn(campaign_id="camp_a", stat_date=utc_today())

        problem = _semantic_problem(empty)

        assert problem is not None and "no measurements" in problem

    def test_a_far_future_date_is_rejected(self) -> None:
        problem = _semantic_problem(
            record(stat_date=utc_today() + timedelta(days=MAX_FUTURE_DAYS + 1))
        )

        assert problem is not None and "ahead of today" in problem

    def test_tomorrow_is_tolerated(self) -> None:
        """A platform reporting from ahead of our timezone is not a bug."""
        assert _semantic_problem(record(stat_date=utc_today() + timedelta(days=1))) is None

    def test_history_is_never_too_old(self) -> None:
        """A backfill is legitimate, so age alone is not a reason to refuse a row."""
        assert _semantic_problem(record(stat_date=date(2019, 1, 1))) is None


class TestCampaignResolution:
    def resolve(
        self, parsed: MetricRecordIn, *, known: set[str], external: dict[tuple[str, str], str]
    ) -> tuple[str | None, str | None]:
        return _resolve_campaign(parsed, known_campaigns=known, external_map=external)

    def test_an_internal_id_is_used_when_it_exists(self) -> None:
        assert self.resolve(record(), known={"camp_a"}, external={}) == ("camp_a", None)

    def test_an_unknown_internal_id_is_reported_not_guessed(self) -> None:
        campaign_id, problem = self.resolve(record(), known=set(), external={})

        assert campaign_id is None
        assert problem is not None and "no campaign has id camp_a" in problem

    def test_platform_coordinates_resolve_to_the_mapped_campaign(self) -> None:
        parsed = record(campaign_id=None, platform="google", external_id="ext_1")

        assert self.resolve(parsed, known=set(), external={("google", "ext_1"): "camp_z"}) == (
            "camp_z",
            None,
        )

    def test_unmapped_platform_coordinates_are_reported(self) -> None:
        parsed = record(campaign_id=None, platform="google", external_id="ext_missing")

        campaign_id, problem = self.resolve(parsed, known=set(), external={})

        assert campaign_id is None
        assert problem is not None and "no campaign matches google/ext_missing" in problem

    def test_agreeing_identities_resolve_once(self) -> None:
        """A warehouse export legitimately carries both, and that is not a conflict."""
        parsed = record(platform="google", external_id="ext_1")

        assert self.resolve(parsed, known={"camp_a"}, external={("google", "ext_1"): "camp_a"}) == (
            "camp_a",
            None,
        )

    def test_disagreeing_identities_are_reported(self) -> None:
        """The mapping has drifted; writing either one silently would hide it."""
        parsed = record(platform="google", external_id="ext_1")

        campaign_id, problem = self.resolve(
            parsed, known={"camp_a"}, external={("google", "ext_1"): "camp_other"}
        )

        assert campaign_id is None
        assert problem is not None and "two different campaigns" in problem

    def test_a_failed_identity_is_not_rescued_by_the_other_one(self) -> None:
        """Both were asserted, so both have to hold."""
        parsed = record(platform="google", external_id="ext_1")

        campaign_id, problem = self.resolve(parsed, known={"camp_a"}, external={})

        assert campaign_id is None
        assert problem is not None and "no campaign matches" in problem

    def test_a_record_addressing_nothing_is_reported_rather_than_dropped(self) -> None:
        """The semantic pass rejects these first; a direct caller still gets an answer."""
        parsed = record(campaign_id=None)

        campaign_id, problem = self.resolve(parsed, known={"camp_a"}, external={})

        assert campaign_id is None
        assert problem == "record could not be resolved to a campaign"


class TestDuplicateSlots:
    def attributed(
        self, index: int, parsed: MetricRecordIn, creative: str | None = None
    ) -> _Attributed:
        return _Attributed(index=index, record=parsed, campaign_id="camp_a", creative_id=creative)

    def test_both_records_in_a_collision_are_rejected(self) -> None:
        rejected: list[IngestIssue] = []
        items = [self.attributed(0, record()), self.attributed(1, record(cost=9.0))]

        kept = _reject_duplicate_slots(items, rejected)

        assert kept == []
        assert sorted(issue.index for issue in rejected) == [0, 1]
        assert "record #1" in rejected[0].reason
        assert "record #0" in rejected[1].reason

    def test_distinct_days_do_not_collide(self) -> None:
        rejected: list[IngestIssue] = []
        items = [
            self.attributed(0, record(stat_date=utc_today())),
            self.attributed(1, record(stat_date=utc_today() - timedelta(days=1))),
        ]

        assert len(_reject_duplicate_slots(items, rejected)) == 2
        assert rejected == []

    def test_a_creative_slot_does_not_collide_with_the_campaign_slot(self) -> None:
        rejected: list[IngestIssue] = []
        items = [self.attributed(0, record()), self.attributed(1, record(), creative="cre_a")]

        assert len(_reject_duplicate_slots(items, rejected)) == 2
        assert rejected == []

    def test_a_clean_record_survives_the_collision_beside_it(self) -> None:
        """Only the ambiguous rows are withheld; the batch is not punished as a whole."""
        rejected: list[IngestIssue] = []
        items = [
            self.attributed(0, record()),
            self.attributed(1, record(cost=9.0)),
            self.attributed(2, record(stat_date=utc_today() - timedelta(days=1))),
        ]

        kept = _reject_duplicate_slots(items, rejected)

        assert [item.index for item in kept] == [2]
        assert sorted(issue.index for issue in rejected) == [0, 1]


class TestConversion:
    def test_both_identity_schemes_survive_the_conversion(self) -> None:
        """So the service can cross-check them instead of trusting one."""
        row = SourceRecord(
            stat_date=WINDOW_START,
            platform=Platform.META,
            external_id="ext_9",
            campaign_id="camp_a",
            creative_id="cre_1",
            impressions=10,
            revenue=None,
        )

        parsed = to_metric_input(row)

        assert parsed.platform is Platform.META
        assert parsed.external_id == "ext_9"
        assert parsed.campaign_id == "camp_a"
        assert parsed.creative_id == "cre_1"
        assert parsed.revenue is None
        assert parsed.impressions == 10
