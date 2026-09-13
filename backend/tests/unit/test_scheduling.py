"""The missed-run policy, and the parts of the scheduler that need no database.

``plan_window`` is the whole argument about whether a schedule can be trusted:
what it asks for when nothing has ever run, when a run was missed, and when the
gap is wider than the configured recovery bound. It is pure on purpose, so every
branch is asserted here as arithmetic against fixed dates rather than as a
snapshot that silently drifts.

The database half - a watermark that only widens, a lease that exactly one caller
can claim - is driven end to end in ``tests/integration/test_scheduler_api.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from adoptimizer.core.config import (
    SOURCE_NAME_PATTERN,
    DatabaseSettings,
    DataMode,
    IngestSettings,
)
from adoptimizer.infra.ads.registry import build_platform_clients
from adoptimizer.infra.db.models import IngestWatermark, SchedulerLease
from adoptimizer.infra.db.session import Database
from adoptimizer.infra.ingest import build_metric_sources
from adoptimizer.schemas.scheduling import (
    LeaseOut,
    PlanReason,
    PullPlanOut,
    TickOut,
    TickReportOut,
    TickResult,
    WatermarkOut,
)
from adoptimizer.services.scheduling import (
    IngestScheduler,
    _lease_out,
    _watermark_out,
    default_holder,
    plan_window,
)

TODAY = date(2026, 9, 9)
LOOKBACK = 3
CATCHUP = 14
FEED = "platform"

# What plan_window derives from the three constants above, written out instead of
# recomputed: if the arithmetic changes, a wrong constant here is the failure.
NOMINAL_START = date(2026, 9, 7)
FLOOR = date(2026, 8, 27)
ONE_DAY = timedelta(days=1)
ONE_SECOND = timedelta(seconds=1)


def plan(
    covered: tuple[date, date] | None = None,
    *,
    today: date = TODAY,
    lookback_days: int = LOOKBACK,
    max_catchup_days: int = CATCHUP,
) -> PullPlanOut:
    """One plan under the fixed constants, so each test states only its premise."""
    return plan_window(
        FEED,
        today=today,
        lookback_days=lookback_days,
        max_catchup_days=max_catchup_days,
        covered=covered,
    )


@pytest.fixture
def scheduler() -> IngestScheduler:
    """A scheduler wired to nothing.

    Construction is inert: the engine is created lazily and the source registry
    is in-memory, so the identity questions below never open a database.
    """
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    sources = build_metric_sources(build_platform_clients(DataMode.MOCK))
    return IngestScheduler(database.session_factory, sources, IngestSettings())


class TestFirstRun:
    """A feed that has never been pulled must not be given a history nobody asked for."""

    def test_the_window_is_the_nominal_lookback(self) -> None:
        result = plan(None)

        assert result.reason is PlanReason.FIRST
        assert (result.start, result.end) == (NOMINAL_START, TODAY)
        assert result.days == LOOKBACK
        assert result.catchup_days == 0
        assert result.gap_days == 0
        assert result.covered is False

    def test_the_detail_says_history_is_a_deliberate_backfill(self) -> None:
        assert "backfill" in plan(None).detail

    def test_a_one_day_lookback_asks_for_exactly_one_day(self) -> None:
        result = plan(None, lookback_days=1, max_catchup_days=1)

        assert (result.start, result.end) == (TODAY, TODAY)
        assert result.days == 1

    def test_the_feed_name_is_carried_through(self) -> None:
        assert plan(None).source == FEED


class TestCovered:
    """Redundancy is proved, not assumed: this is what makes re-running cheap."""

    def test_an_exact_match_is_skipped(self) -> None:
        result = plan((NOMINAL_START, TODAY))

        assert result.reason is PlanReason.COVERED
        assert result.covered is True
        assert result.days == LOOKBACK

    def test_a_wider_stored_span_still_reports_the_nominal_window(self) -> None:
        """The plan describes what a pull would ask for, not what is stored."""
        result = plan((date(2026, 1, 1), date(2026, 12, 31)))

        assert result.reason is PlanReason.COVERED
        assert (result.start, result.end) == (NOMINAL_START, TODAY)

    def test_a_watermark_ahead_of_today_counts_as_covered(self) -> None:
        """Clock skew or a backfill that ran ahead must not trigger a pull."""
        assert plan((NOMINAL_START, date(2026, 9, 20))).reason is PlanReason.COVERED

    def test_a_span_that_starts_late_is_not_covered(self) -> None:
        """Covering today is not the same as covering the whole lookback."""
        assert plan((date(2026, 9, 8), TODAY)).reason is PlanReason.NOMINAL


class TestNominal:
    """The ordinary case: yesterday is stored, so the overlap re-checks it."""

    def test_a_watermark_ending_yesterday_overlaps_the_lookback(self) -> None:
        result = plan((date(2026, 9, 1), date(2026, 9, 8)))

        assert result.reason is PlanReason.NOMINAL
        assert (result.start, result.end) == (NOMINAL_START, TODAY)
        assert result.catchup_days == 0

    def test_the_boundary_where_catchup_becomes_nominal(self) -> None:
        """One day earlier and the same window would be a catch-up."""
        just_before_nominal = plan((date(2026, 9, 1), NOMINAL_START - ONE_DAY))

        assert just_before_nominal.reason is PlanReason.NOMINAL


class TestCatchup:
    """A missed run is closed by the next one, and says how far it reached."""

    def test_a_missed_run_is_closed_by_the_next_one(self) -> None:
        result = plan((date(2026, 8, 20), date(2026, 9, 2)))

        assert result.reason is PlanReason.CATCHUP
        assert (result.start, result.end) == (date(2026, 9, 3), TODAY)
        assert result.days == 7
        assert result.gap_days == 0

    def test_catchup_reports_the_days_it_reached_past_the_lookback(self) -> None:
        assert plan((date(2026, 8, 20), date(2026, 9, 2))).catchup_days == 4

    def test_the_recovery_bound_itself_is_still_a_catchup(self) -> None:
        """Off by one here would report a healthy run as owing a backfill."""
        result = plan((date(2026, 7, 1), FLOOR - ONE_DAY))

        assert result.reason is PlanReason.CATCHUP
        assert result.start == FLOOR
        assert result.days == CATCHUP
        assert result.gap_days == 0

    def test_the_detail_names_the_watermark_it_is_continuing_from(self) -> None:
        assert "2026-09-02" in plan((date(2026, 8, 20), date(2026, 9, 2))).detail


class TestCapped:
    """Beyond the bound the plan refuses to guess and names the fix instead."""

    def test_a_gap_wider_than_the_bound_stops_at_the_bound(self) -> None:
        result = plan((date(2026, 7, 1), date(2026, 8, 10)))

        assert result.reason is PlanReason.CAPPED
        assert result.start == FLOOR
        assert result.end == TODAY
        assert result.days == CATCHUP

    def test_the_days_still_owed_are_reported_rather_than_absorbed(self) -> None:
        result = plan((date(2026, 7, 1), date(2026, 8, 10)))

        # The uncovered span is 2026-08-11..2026-08-26; the window starts 08-27.
        assert result.gap_days == 16
        assert result.catchup_days == 11

    def test_the_detail_carries_the_exact_backfill_command(self) -> None:
        detail = plan((date(2026, 7, 1), date(2026, 8, 10))).detail

        assert "adoptimizer ingest --start 2026-08-11 --end 2026-08-26" in detail

    def test_a_wider_bound_admits_a_wider_window(self) -> None:
        result = plan(
            (date(2026, 7, 1), date(2026, 8, 10)),
            lookback_days=LOOKBACK,
            max_catchup_days=40,
        )

        assert result.reason is PlanReason.CATCHUP
        assert result.start == date(2026, 8, 11)
        assert result.gap_days == 0


class TestIdentity:
    """Who is pulling, and under which lock."""

    def test_the_holder_names_the_process(self) -> None:
        holder = default_holder()

        assert holder.endswith(":" + str(os.getpid()))
        assert holder.count(":") >= 1

    def test_an_explicit_holder_is_kept(self, scheduler: IngestScheduler) -> None:
        settings = IngestSettings()
        sources = build_metric_sources(build_platform_clients(DataMode.MOCK))
        database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
        instance = IngestScheduler(database.session_factory, sources, settings, holder="pod-a:1234")

        assert instance.holder == "pod-a:1234"
        assert instance.settings is settings

    def test_the_lease_is_one_per_feed(self, scheduler: IngestScheduler) -> None:
        assert scheduler.lease_name("platform") == "ingest:platform"
        assert scheduler.lease_name("synthetic") == "ingest:synthetic"

    def test_the_default_holder_is_used_when_none_is_given(
        self, scheduler: IngestScheduler
    ) -> None:
        assert scheduler.holder == default_holder()


class TestDerivedFields:
    """A watermark's age and a lease's held-ness both go stale the moment they are stored.

    They are computed on the way out rather than read from a column, so these
    assert the arithmetic - including the SQLite case, where a
    ``DateTime(timezone=True)`` column hands back a naive value that would raise
    on comparison against an aware ``now``.

    Every row below spells out the columns the database fills in on insert. These
    are the values a persisted row always has, and stating them keeps the tests
    about the derivation instead of about SQLAlchemy's flush timing.
    """

    def test_a_watermark_reports_its_age_in_days(self) -> None:
        row = IngestWatermark(
            source=FEED,
            window_start=TODAY - timedelta(days=9),
            window_end=TODAY - timedelta(days=4),
            batch_id="ing_1",
            received=12,
            dry_run=False,
            updated_at=datetime(2026, 9, 5, 6, 10, tzinfo=UTC),
        )

        out = _watermark_out(row, today=TODAY)

        assert out.lag_days == 4
        assert out.updated_at == datetime(2026, 9, 5, 6, 10, tzinfo=UTC)
        assert out.batch_id == "ing_1"
        assert out.received == 12
        assert out.dry_run is False

    def test_a_naive_timestamp_is_tagged_rather_than_converted(self) -> None:
        """Tagging, not converting: every stored instant is already UTC wall clock."""
        # SQLite ignores DateTime(timezone=True) and hands the value back naive.
        naive = datetime(2026, 9, 9, 12, 0, tzinfo=UTC).replace(tzinfo=None)
        row = IngestWatermark(
            source=FEED,
            window_start=TODAY,
            window_end=TODAY,
            received=0,
            dry_run=False,
            updated_at=naive,
        )

        out = _watermark_out(row, today=TODAY)

        assert out.updated_at is not None
        assert out.updated_at.tzinfo is UTC
        assert out.updated_at.hour == 12

    def test_a_row_whose_timestamp_is_missing_reports_no_age(self) -> None:
        """``updated_at`` is nullable in the model, so the derivation has to cope."""
        row = IngestWatermark(
            source=FEED, window_start=TODAY, window_end=TODAY, received=0, dry_run=False
        )

        out = _watermark_out(row, today=TODAY)

        assert out.updated_at is None
        assert out.lag_days == 0

    def test_a_lease_with_a_future_expiry_is_held(self) -> None:
        now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        row = SchedulerLease(
            name="ingest:" + FEED,
            holder="pod-a:1",
            acquired_at=now,
            expires_at=now + timedelta(seconds=600),
            last_outcome="",
            last_message="",
            consecutive_failures=0,
        )

        out = _lease_out(row, now=now)

        assert out.held is True
        assert out.holder == "pod-a:1"
        assert out.acquired_at == now

    def test_a_lease_that_expired_a_second_ago_is_not_held(self) -> None:
        now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        row = SchedulerLease(
            name="ingest:" + FEED,
            holder="pod-a:1",
            expires_at=now - ONE_SECOND,
            last_outcome="",
            last_message="",
            consecutive_failures=0,
        )

        assert _lease_out(row, now=now).held is False

    def test_a_released_lease_keeps_the_history_of_its_last_holder(self) -> None:
        """Deleting the row would cost the answer to "who ran this last"."""
        row = SchedulerLease(
            name="ingest:" + FEED,
            holder="",
            last_outcome="ran",
            last_message="m",
            last_batch_id="ing_9",
            last_finished_at=datetime(2026, 9, 9, 6, 10, tzinfo=UTC),
            consecutive_failures=0,
        )

        out = _lease_out(row, now=datetime(2026, 9, 9, 12, 0, tzinfo=UTC))

        assert out.held is False
        assert out.expires_at is None
        assert out.acquired_at is None
        assert out.last_outcome == "ran"
        assert out.last_batch_id == "ing_9"
        assert out.consecutive_failures == 0


class TestContracts:
    """The wire vocabulary, pinned so a rename breaks a test and not a dashboard."""

    def test_the_plan_reasons_are_fixed(self) -> None:
        assert {member.value for member in PlanReason} == {
            "covered",
            "first",
            "nominal",
            "catchup",
            "capped",
        }

    def test_the_tick_outcomes_are_fixed(self) -> None:
        assert {member.value for member in TickResult} == {
            "ran",
            "skipped",
            "lost_lease",
            "failed",
        }

    def test_a_skip_is_a_healthy_pass(self) -> None:
        report = TickReportOut(ticks=[], skipped=2)

        assert report.ok is True

    def test_a_lost_lease_is_a_healthy_pass(self) -> None:
        assert TickReportOut(ticks=[], lost_lease=1).ok is True

    def test_only_a_failure_makes_a_pass_unhealthy(self) -> None:
        assert TickReportOut(ticks=[], ran=1, failed=1).ok is False

    def test_an_empty_report_is_ok(self) -> None:
        assert TickReportOut().ok is True

    def test_a_tick_defaults_to_the_shape_of_a_skip(self) -> None:
        tick = TickOut(source=FEED, outcome=TickResult.SKIPPED)

        assert tick.received == 0
        assert tick.batch_id is None
        assert tick.error is None

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            (
                PullPlanOut,
                {
                    "source": FEED,
                    "start": TODAY,
                    "end": TODAY,
                    "days": 1,
                    "reason": PlanReason.FIRST,
                    "detail": "d",
                },
            ),
            (WatermarkOut, {"source": FEED, "window_start": TODAY, "window_end": TODAY}),
            (LeaseOut, {"name": "ingest:platform"}),
        ],
    )
    def test_the_shapes_refuse_a_field_they_do_not_define(
        self, model: type[object], payload: dict[str, object]
    ) -> None:
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "surprise": 1})  # type: ignore[attr-defined]


class TestSourceNamePolicy:
    """A feed name becomes a metric label and a column value, so its shape is bounded."""

    def test_the_pattern_is_exported_for_the_wire_contract(self) -> None:
        from adoptimizer.schemas.ingest import SOURCE_PATTERN

        assert SOURCE_PATTERN == SOURCE_NAME_PATTERN

    def test_a_default_settings_object_pulls_the_demo_feed(self) -> None:
        settings = IngestSettings()

        assert settings.sources == ["synthetic"]
        assert settings.scheduler_enabled is True
        assert settings.dry_run is False
        assert settings.max_catchup_days >= settings.lookback_days
