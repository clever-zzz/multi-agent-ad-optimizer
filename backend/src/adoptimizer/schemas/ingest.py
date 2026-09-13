"""Ingestion contracts.

Validation is split in two on purpose, because the two kinds of wrong mean
different things to whoever is on call:

- **Structural** errors are rejected by Pydantic and fail the whole request with
  a 422: a record that is not an object, a ``stat_date`` that is not a date, a
  field name we do not know. These mean the producer is broken, and quietly
  dropping 5,000 rows because one key was renamed is how a feed silently starts
  writing zeros. ``extra="forbid"`` is what makes the rename visible.
- **Semantic** errors are reported per record by the service and never abort the
  batch: a negative spend, a date in the future, a record that measures nothing,
  a campaign we have never seen. These mean one row is wrong, and the other 4,999
  are still worth having. Note that the numeric fields therefore carry no ``ge=0``
  constraint - a bound declared here would be enforced by Pydantic and would take
  the whole batch down with the bad row.

Anything in the second category shows up in ``rejected`` or ``unresolved`` with
its index, so a partial feed is auditable instead of merely smaller.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.config import SOURCE_NAME_PATTERN
from ..domain.enums import Platform
from ..domain.kpi import DAILY_MEASUREMENTS

# A batch is one transaction. Capping it bounds the write amplification and the
# size of a report, and forces a producer that has more to say to page - which
# is what we want anyway, since a partial page still lands.
MAX_BATCH_RECORDS = 5_000

# Reporting every failure of a 5,000-row batch would produce a response larger
# than the data it describes. The counts stay exact; only the itemised lists are
# capped.
MAX_ISSUES_REPORTED = 200

# Source names become a Prometheus label and two database columns, so they are
# constrained to a bounded, low-cardinality shape rather than free text. The
# pattern itself lives in ``core.config`` because ``INGEST__SOURCES`` has to
# satisfy exactly the same rule; aliasing it here keeps one definition and this
# module's public name unchanged.
SOURCE_PATTERN = SOURCE_NAME_PATTERN


class MetricRecordIn(BaseModel):
    """One daily aggregate asserted by a feed.

    A record addresses its campaign either by the platform coordinates a feed
    naturally has (``platform`` + ``external_id``) or by the internal
    ``campaign_id``. Exactly one scheme must be used; supplying both is rejected
    rather than resolved by precedence, because a mismatch between them is a bug
    in the producer and silently picking one hides it.

    ``creative_id`` is internal-only. Creatives carry no ``external_id`` in this
    schema, so a platform feed cannot name one and must leave it null, which
    writes the campaign-level slot.
    """

    model_config = ConfigDict(extra="forbid")

    platform: Platform | None = None
    external_id: str | None = Field(default=None, max_length=120)
    campaign_id: str | None = Field(default=None, max_length=40)
    creative_id: str | None = Field(default=None, max_length=40)
    stat_date: date

    # Every measurement is optional, and absent is not zero. An ad network has
    # never heard of your revenue and a commerce webhook has never heard of your
    # reach; a feed that cannot measure a column omits it, and the writer leaves
    # the stored value alone. Defaulting these to 0 would make an honest feed
    # overwrite real numbers with fabrications.
    impressions: int | None = None
    clicks: int | None = None
    conversions: int | None = None
    cost: float | None = None
    revenue: float | None = None
    unique_reach: int | None = None

    @field_validator("impressions", "clicks", "conversions", "unique_reach", mode="before")
    @classmethod
    def _round_counts(cls, value: object) -> object:
        """Round a fractional count instead of failing the batch on it.

        Google Ads reports conversions as a double and a CSV-shaped export
        routinely carries "1234.0", so a strict integer field here would reject an
        entire feed over one row. The columns are integers, so the value has to
        be rounded somewhere; doing it at the edge, half away from zero, matches
        how the platforms display it and keeps the stored number comparable to
        what an operator sees in the ad console.
        """
        if isinstance(value, float):
            return math.floor(value + 0.5) if value >= 0 else -math.floor(-value + 0.5)
        return value

    @field_validator("external_id", "campaign_id", "creative_id")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        """Treat an empty string as unset.

        Feeds built from spreadsheets and CSV exports hand us "" for every
        column they do not populate. Without this, "" would look like a supplied
        identity and the addressing check below would pass on a record that has
        no way to be resolved.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def measurements_present(self) -> tuple[str, ...]:
        """Which measured columns this record actually asserts.

        An empty result means the record says nothing at all, and writing it
        would create an all-zero row that looks like a day with no activity.
        """
        return tuple(name for name in DAILY_MEASUREMENTS if getattr(self, name) is not None)

    def identity(self) -> str:
        """A short, log-safe description of what this record points at."""
        if self.campaign_id:
            return "campaign_id=" + self.campaign_id
        if self.platform is not None and self.external_id:
            return self.platform.value + "/" + self.external_id
        return "unaddressed"


class IngestBatchIn(BaseModel):
    """A push of metric records from one named feed."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=SOURCE_PATTERN)
    dry_run: bool = False
    records: list[MetricRecordIn] = Field(min_length=1, max_length=MAX_BATCH_RECORDS)

    @field_validator("source", mode="before")
    @classmethod
    def _normalise_source(cls, value: object) -> object:
        """Lower-case and trim before the pattern is applied.

        Runs in "before" mode on purpose: in the default "after" mode the pattern
        constraint fires first and a producer that capitalises its own name is
        rejected for a difference that carries no meaning.
        """
        return value.strip().lower() if isinstance(value, str) else value


class IngestIssue(BaseModel):
    """One record that did not land, and why."""

    index: int = Field(ge=0)
    reason: str
    identity: str = ""
    stat_date: date | None = None


class IngestReportOut(BaseModel):
    """What an ingestion attempt did.

    The counts always add up: ``received == created + updated + rejected +
    unresolved`` for a write, and for a dry run they describe what *would* have
    happened. A report whose numbers do not reconcile is the first thing an
    operator should distrust, so the service asserts it before returning.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str | None = None
    source: str
    dry_run: bool = False
    received: int = 0
    created: int = 0
    updated: int = 0
    rejected_count: int = 0
    unresolved_count: int = 0
    rejected: list[IngestIssue] = Field(default_factory=list)
    unresolved: list[IngestIssue] = Field(default_factory=list)
    issues_truncated: bool = False
    window_start: date | None = None
    window_end: date | None = None

    @property
    def accepted(self) -> int:
        return self.created + self.updated

    def reconciles(self) -> None:
        """Assert the accounting identity the report promises."""
        accounted = self.created + self.updated + self.rejected_count + self.unresolved_count
        if self.received != accounted:
            msg = (
                "ingest report does not reconcile: received="
                + str(self.received)
                + " created="
                + str(self.created)
                + " updated="
                + str(self.updated)
                + " rejected="
                + str(self.rejected_count)
                + " unresolved="
                + str(self.unresolved_count)
            )
            raise AssertionError(msg)


class IngestBatchOut(BaseModel):
    """One recorded ingestion attempt.

    This is the answer to "did the feed arrive": it exists for dry runs too, so a
    rehearsal is distinguishable from a pipeline that never ran.

    The two counts are aliased onto the stored column names so that a count is
    called ``*_count`` and a bare ``rejected`` means the itemised list on every
    surface this tag exposes. A client should not have to know which endpoint it
    is reading to know whether a field is a number or a list.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    source: str
    actor: str = ""
    received: int = 0
    created: int = 0
    updated: int = 0
    rejected_count: int = Field(default=0, validation_alias="rejected")
    unresolved_count: int = Field(default=0, validation_alias="unresolved")
    dry_run: bool = False
    window_start: date | None = None
    window_end: date | None = None
    created_at: datetime | None = None


class IngestSourceOut(BaseModel):
    """A registered feed and whether it could actually be pulled right now."""

    name: str
    registered: bool = True
    configured: bool = False


class IngestSourceStatusOut(BaseModel):
    """Every feed this deployment knows about."""

    sources: list[IngestSourceOut] = Field(default_factory=list)
    configured: list[str] = Field(default_factory=list)


__all__ = [
    "MAX_BATCH_RECORDS",
    "MAX_ISSUES_REPORTED",
    "SOURCE_PATTERN",
    "IngestBatchIn",
    "IngestBatchOut",
    "IngestIssue",
    "IngestReportOut",
    "IngestSourceOut",
    "IngestSourceStatusOut",
    "MetricRecordIn",
]
