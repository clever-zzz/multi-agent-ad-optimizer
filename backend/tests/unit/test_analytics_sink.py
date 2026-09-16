"""The warehouse write path: normalisation, batching and honest reporting.

The read path degrades to an empty result when ClickHouse is unreachable. These
tests pin down the opposite stance on the write side, because the failure mode
worth designing against is a backfill that reports success over a warehouse that
received nothing.

Two behaviours are load-bearing and get their own regression tests:

* A row that does not fit a column type is **refused and counted**, never
  coerced. ``ad_events`` declares ``Enum8`` columns, so ClickHouse would reject
  the whole batch with an error naming the column but not the row.
* A write against a sink that is not connected **raises**. Silently succeeding
  would lose data that nothing else holds a copy of.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from adoptimizer.core.config import ClickHouseSettings
from adoptimizer.core.errors import ExternalServiceError
from adoptimizer.domain.enums import EventType
from adoptimizer.infra.analytics import (
    REASON_NO_CAMPAIGN,
    REASON_NO_EVENT_ID,
    REASON_SINK_DISABLED,
    REASON_UNKNOWN_DEVICE,
    REASON_UNKNOWN_EVENT_TYPE,
    REASON_UNKNOWN_GENDER,
    REASON_UNKNOWN_PLATFORM,
    AdEvent,
    ClickHouseSink,
    DailyMetricRow,
    NullSink,
    build_sink,
    daily_row,
    normalise_event,
)

EVENT_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def an_event(**overrides: Any) -> AdEvent:
    payload: dict[str, Any] = {
        "event_id": "evt_1",
        "campaign_id": "cmp_1",
        "event_type": EventType.IMPRESSION,
        "event_time": EVENT_TIME,
        "creative_id": "cre_1",
        "cost": 0.01,
        "revenue": 0.0,
        "platform": "google",
        "device": "mobile",
        "country": "CN",
        "age_group": "25-34",
        "gender": "female",
    }
    payload.update(overrides)
    return AdEvent(**payload)


class FakeSinkDriver:
    """Mimics clickhouse_connect's insert/command surface."""

    def __init__(self, *, fail_insert: bool = False, fail_command: bool = False) -> None:
        self.fail_insert = fail_insert
        self.fail_command = fail_command
        self.inserts: list[tuple[str, list[tuple[Any, ...]], list[str]]] = []
        self.commands: list[str] = []
        self.closed = False

    def insert(self, table: str, data: Any, column_names: list[str] | None = None) -> None:
        self.inserts.append((table, list(data), list(column_names or [])))
        if self.fail_insert:
            raise RuntimeError("clickhouse is on fire")

    def command(self, sql: str) -> str:
        self.commands.append(sql)
        if self.fail_command:
            raise RuntimeError("clickhouse is on fire")
        return "1"

    def close(self) -> None:
        self.closed = True

    @property
    def rows_written(self) -> int:
        return sum(len(rows) for _table, rows, _columns in self.inserts)


def connected_sink(
    *, fail_insert: bool = False, fail_command: bool = False
) -> tuple[ClickHouseSink, FakeSinkDriver]:
    """A sink whose driver is already wired up, skipping connect()."""
    settings = ClickHouseSettings(enabled=True, database="ad_optimizer")
    sink = ClickHouseSink(settings)
    driver = FakeSinkDriver(fail_insert=fail_insert, fail_command=fail_command)
    sink._client = driver
    return sink, driver


class TestEventNormalisation:
    def test_a_well_formed_event_maps_onto_every_column(self) -> None:
        row, reason = normalise_event(an_event())

        assert reason == ""
        assert row is not None
        assert row["event_id"] == "evt_1"
        assert row["event_type"] == "impression"
        assert row["platform"] == "google"
        assert row["device"] == "mobile"
        assert row["event_time"] == EVENT_TIME

    def test_a_mock_platform_is_refused(self) -> None:
        """The warehouse models the three real networks; a mock row would be a lie."""
        row, reason = normalise_event(an_event(platform="mock"))

        assert row is None
        assert reason == REASON_UNKNOWN_PLATFORM

    def test_an_unmodelled_device_is_refused_rather_than_coerced(self) -> None:
        """``ad_events`` has no ``unknown`` device, but inventing one is worse."""
        row, reason = normalise_event(an_event(device="ctv"))

        assert row is None
        assert reason == REASON_UNKNOWN_DEVICE

    def test_an_unmodelled_gender_is_refused(self) -> None:
        row, reason = normalise_event(an_event(gender="nonbinary"))

        assert row is None
        assert reason == REASON_UNKNOWN_GENDER

    def test_an_unmodelled_event_type_is_refused(self) -> None:
        row, reason = normalise_event(an_event(event_type="view_through"))  # type: ignore[arg-type]

        assert row is None
        assert reason == REASON_UNKNOWN_EVENT_TYPE

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"event_id": "  "}, REASON_NO_EVENT_ID),
            ({"campaign_id": ""}, REASON_NO_CAMPAIGN),
        ],
    )
    def test_missing_identity_is_refused(self, overrides: dict[str, Any], expected: str) -> None:
        row, reason = normalise_event(an_event(**overrides))

        assert row is None
        assert reason == expected

    def test_values_are_lowercased_so_a_casing_difference_does_not_split_a_group(
        self,
    ) -> None:
        row, reason = normalise_event(an_event(platform="Google", device="Mobile"))

        assert reason == ""
        assert row is not None
        assert row["platform"] == "google"
        assert row["device"] == "mobile"


class TestDailyRow:
    def test_an_empty_creative_id_becomes_an_empty_string(self) -> None:
        """The table declares the column as non-nullable String with a '' default."""
        row = daily_row(
            DailyMetricRow(campaign_id="cmp_1", stat_date=date(2026, 9, 1), impressions=10)
        )

        assert row["creative_id"] == ""
        assert row["impressions"] == 10

    def test_money_is_rounded_to_four_places(self) -> None:
        row = daily_row(
            DailyMetricRow(
                campaign_id="cmp_1",
                stat_date=date(2026, 9, 1),
                cost=12.3456789,
                revenue=0.1 + 0.2,
            )
        )

        assert row["cost"] == 12.3457
        assert row["revenue"] == 0.3

    def test_an_absent_reach_stays_none_rather_than_becoming_zero(self) -> None:
        """Zero would assert a measured reach of nothing; None says unmeasured."""
        row = daily_row(DailyMetricRow(campaign_id="cmp_1", stat_date=date(2026, 9, 1)))

        assert row["unique_reach"] is None


class TestClickHouseSink:
    async def test_events_are_inserted_with_explicit_column_names(self) -> None:
        sink, driver = connected_sink()

        report = await sink.write_events([an_event(), an_event(event_id="evt_2")])

        assert report.written == 2
        assert report.skipped == 0
        assert report.table == "ad_events"

        table, rows, columns = driver.inserts[0]
        assert table == "ad_events"
        assert columns == list(ClickHouseSink.EVENT_COLUMNS)
        # Column order must match the declared names, or the values land in the
        # wrong columns.
        assert rows[0][columns.index("event_id")] == "evt_1"
        assert rows[0][columns.index("platform")] == "google"

    async def test_refused_rows_are_counted_by_reason_not_dropped(self) -> None:
        sink, driver = connected_sink()

        report = await sink.write_events(
            [
                an_event(),
                an_event(event_id="evt_2", platform="mock"),
                an_event(event_id="evt_3", platform="mock"),
                an_event(event_id="evt_4", device="ctv"),
            ]
        )

        assert report.written == 1
        assert report.skipped == 3
        assert report.reasons[REASON_UNKNOWN_PLATFORM] == 2
        assert report.reasons[REASON_UNKNOWN_DEVICE] == 1
        # Only the storable row reached the driver.
        assert driver.rows_written == 1

    async def test_a_batch_larger_than_the_chunk_size_is_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ClickHouseSink, "BATCH_SIZE", 3)
        sink, driver = connected_sink()

        report = await sink.write_events([an_event(event_id=f"evt_{i}") for i in range(7)])

        assert report.written == 7
        assert [len(rows) for _table, rows, _columns in driver.inserts] == [3, 3, 1]

    async def test_an_empty_write_does_not_touch_the_driver(self) -> None:
        sink, driver = connected_sink()

        report = await sink.write_events([])

        assert report.written == 0
        assert driver.inserts == []

    async def test_a_write_without_a_connection_raises(self) -> None:
        """Silently succeeding here would lose data nothing else holds."""
        sink = ClickHouseSink(ClickHouseSettings(enabled=True))

        with pytest.raises(ExternalServiceError, match="not connected"):
            await sink.write_events([an_event()])

    async def test_an_insert_failure_propagates(self) -> None:
        sink, _ = connected_sink(fail_insert=True)

        with pytest.raises(RuntimeError, match="on fire"):
            await sink.write_events([an_event()])

    async def test_daily_rows_are_never_refused(self) -> None:
        """The aggregate table declares its dimensions as String, on purpose."""
        sink, driver = connected_sink()

        report = await sink.write_daily(
            [
                DailyMetricRow(campaign_id="cmp_1", stat_date=date(2026, 9, 1), impressions=5),
                DailyMetricRow(campaign_id="cmp_1", stat_date=date(2026, 9, 2), impressions=7),
            ]
        )

        assert report.written == 2
        assert report.skipped == 0
        assert driver.inserts[0][0] == "campaign_daily_metrics"

    async def test_healthcheck_reports_ok_error_and_unavailable(self) -> None:
        healthy, healthy_driver = connected_sink()
        broken, _ = connected_sink(fail_command=True)
        never_connected = ClickHouseSink(ClickHouseSettings(enabled=True))

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
        sink, driver = connected_sink()

        await sink.close()
        assert driver.closed is True

        await sink.close()
        assert await sink.healthcheck() == {
            "backend": "clickhouse",
            "status": "unavailable",
        }

    async def test_connect_reports_failure_when_the_driver_is_not_installed(self) -> None:
        """The clickhouse extra is optional; a missing driver is not an error."""
        sink = ClickHouseSink(ClickHouseSettings(enabled=True))

        assert await sink.connect() is False
        assert sink.is_available is False


class TestNullSink:
    async def test_every_row_is_reported_as_skipped_with_a_reason(self) -> None:
        """The failure to make visible is a backfill printing 'done' over nothing."""
        sink = NullSink()

        events = await sink.write_events([an_event(), an_event()])
        daily = await sink.write_daily(
            [DailyMetricRow(campaign_id="cmp_1", stat_date=date(2026, 9, 1))]
        )

        assert sink.is_available is False
        assert events.written == 0
        assert events.skipped == 2
        assert events.reasons == {REASON_SINK_DISABLED: 2}
        assert daily.written == 0
        assert daily.skipped == 1
        assert daily.table == "campaign_daily_metrics"

    async def test_an_empty_write_reports_no_reasons(self) -> None:
        sink = NullSink()

        report = await sink.write_events([])

        assert report.attempted == 0
        assert report.reasons == {}

    async def test_healthcheck_names_the_disabled_sink(self) -> None:
        sink = NullSink("clickhouse_disabled")

        health = await sink.healthcheck()

        assert health["status"] == "disabled"
        assert health["reason"] == "clickhouse_disabled"
        assert await sink.close() is None


class TestBuildSink:
    async def test_disabled_settings_yield_a_null_sink(self) -> None:
        sink = await build_sink(ClickHouseSettings(enabled=False))

        assert isinstance(sink, NullSink)
        assert await sink.healthcheck() == {
            "backend": "null",
            "status": "disabled",
            "reason": "clickhouse_disabled",
        }

    async def test_an_unreachable_clickhouse_yields_a_null_sink(self) -> None:
        """Misconfigured analytics must not block the deployment from starting."""
        sink = await build_sink(ClickHouseSettings(enabled=True))

        assert isinstance(sink, NullSink)
        assert (await sink.healthcheck())["reason"] == "clickhouse_unreachable"

    async def test_a_reachable_clickhouse_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def always_connects(self: ClickHouseSink) -> bool:
            self._client = FakeSinkDriver()
            return True

        monkeypatch.setattr(ClickHouseSink, "connect", always_connects)

        sink = await build_sink(ClickHouseSettings(enabled=True))

        assert isinstance(sink, ClickHouseSink)
        assert sink.is_available is True
        await sink.close()
