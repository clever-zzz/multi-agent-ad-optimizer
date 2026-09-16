"""Metric ingestion.

Turns the rows a feed asserts into stored daily aggregates, and reports exactly
what happened to every one of them.

Three rules shape this module:

1. **Nothing is silently dropped.** A record that fails validation or cannot be
   attributed to a campaign appears in the report with its index and a reason.
   A feed that quietly loses rows produces a dashboard that is wrong in a way
   nobody can see, and by the time anyone notices the numbers are history.
2. **Attribution is never guessed.** Identity is resolved against
   ``uq_campaign_platform_external`` or the internal id. A record that names two
   campaigns, or names one we do not have, is reported as unresolved rather than
   written somewhere plausible.
3. **The report reconciles.** ``received == created + updated + rejected +
   unresolved``, asserted before the report is returned. A report that does not
   add up is the first thing an operator should distrust, so it never leaves
   this module.

The service flushes but does not commit. The batch row, the metric rows and the
audit entry are one unit of work and the caller decides when it becomes durable,
which is what lets an endpoint and the CLI share this code without one of them
inheriting the other's transaction policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utc_today
from ..core.logging import get_logger
from ..core.metrics import INGEST_BATCHES_TOTAL, INGEST_RECORDS_TOTAL
from ..core.security import TokenClaims
from ..domain.enums import Platform
from ..domain.kpi import DAILY_MEASUREMENTS
from ..infra.ingest import MetricSourceRegistry, SourceRecord, SourceTarget
from ..repositories.campaigns import CampaignRepository, CreativeRepository, MetricRepository
from ..repositories.ingest import IngestBatchRepository
from ..schemas.ingest import (
    MAX_ISSUES_REPORTED,
    IngestIssue,
    IngestReportOut,
    MetricRecordIn,
)
from .audit import AuditService

logger = get_logger(__name__)

# A feed asserting tomorrow is broken, but "today" legitimately differs by a day
# across timezones and a platform reporting from UTC+14 is not a bug. One day of
# grace covers that without letting a mis-set clock write future-dated rows the
# optimizer would then treat as history.
MAX_FUTURE_DAYS = 1

# Dispositions reported to ingest_records_total. Dry runs get their own labels
# rather than sharing them: counting a rehearsal as a write would make the
# counter lie about how much data actually landed.
_DRY_PREFIX = "dry_"


@dataclass(frozen=True, slots=True)
class _Attributed:
    """A record that passed validation and was resolved to real rows."""

    index: int
    record: MetricRecordIn
    campaign_id: str
    creative_id: str | None

    @property
    def slot(self) -> tuple[str, str | None, date]:
        return (self.campaign_id, self.creative_id, self.record.stat_date)


def to_metric_input(record: SourceRecord) -> MetricRecordIn:
    """Convert a pulled row into the wire contract.

    Both identity schemes are carried across when the source supplied both. The
    service cross-checks them, which is more useful than picking one here: a
    source whose internal id and platform id disagree is broken, and dropping
    one of the two would hide it.
    """
    return MetricRecordIn(
        platform=record.platform,
        external_id=record.external_id,
        campaign_id=record.campaign_id,
        creative_id=record.creative_id,
        stat_date=record.stat_date,
        impressions=record.impressions,
        clicks=record.clicks,
        conversions=record.conversions,
        cost=record.cost,
        revenue=record.revenue,
        unique_reach=record.unique_reach,
    )


class IngestService:
    """Validates, attributes and writes metric records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._creatives = CreativeRepository(session)
        self._metrics = MetricRepository(session)
        self._batches = IngestBatchRepository(session)

    async def targets(self, *, limit: int = 500) -> list[SourceTarget]:
        """Campaigns a pull source can be asked about.

        Campaigns whose stored platform is not one we know are skipped rather
        than passed through: handing a source a platform it cannot address turns
        a data problem into an exception halfway through a fetch.
        """
        known = {member.value for member in Platform}
        result: list[SourceTarget] = []
        for campaign in await self._campaigns.syncable(limit=limit):
            external_id = (campaign.external_id or "").strip()
            if not external_id or campaign.platform not in known:
                continue
            result.append(
                SourceTarget(
                    platform=Platform(campaign.platform),
                    external_id=external_id,
                    campaign_id=campaign.id,
                )
            )
        return result

    async def pull(
        self,
        sources: MetricSourceRegistry,
        name: str,
        *,
        start: date,
        end: date,
        dry_run: bool = False,
        actor: str = "",
        claims: TokenClaims | None = None,
        client: dict[str, str] | None = None,
    ) -> IngestReportOut:
        """Fetch from a registered source and ingest what it returns.

        A source that raises is allowed to propagate. "The feed errored" and
        "the feed had nothing to report" are different facts, and turning the
        first into an empty report is how a broken pipeline looks healthy.
        """
        source = sources.get(name)
        targets = await self.targets()
        records = await source.fetch(start=start, end=end, targets=targets)
        logger.info(
            "ingest_pulled",
            source=name,
            targets=len(targets),
            records=len(records),
            dry_run=dry_run,
        )
        return await self.ingest(
            [to_metric_input(record) for record in records],
            source=name,
            dry_run=dry_run,
            actor=actor,
            claims=claims,
            client=client,
        )

    async def ingest(
        self,
        records: Sequence[MetricRecordIn],
        *,
        source: str,
        dry_run: bool = False,
        actor: str = "",
        claims: TokenClaims | None = None,
        client: dict[str, str] | None = None,
    ) -> IngestReportOut:
        """Validate, attribute and write one batch of records."""
        received = len(records)
        rejected: list[IngestIssue] = []
        unresolved: list[IngestIssue] = []

        validated: list[tuple[int, MetricRecordIn]] = []
        for index, record in enumerate(records):
            problem = _semantic_problem(record)
            if problem is not None:
                rejected.append(_issue(index, record, problem))
                continue
            validated.append((index, record))

        attributed = _reject_duplicate_slots(await self._attribute(validated, unresolved), rejected)

        dates = sorted({record.stat_date for record in records})
        claimed_start = dates[0] if dates else None
        claimed_end = dates[-1] if dates else None

        # The batch row is opened before the writes because the metric rows carry
        # its id, and closed after them because it carries their counts. One row,
        # two flushes, one transaction.
        batch = await self._batches.record(
            source=source,
            actor=actor,
            received=received,
            created=0,
            updated=0,
            rejected=0,
            unresolved=0,
            dry_run=dry_run,
            window_start=claimed_start,
            window_end=claimed_end,
        )

        known = await self._metrics.existing_slots(
            sorted({item.campaign_id for item in attributed}),
            sorted({item.record.stat_date for item in attributed}),
        )

        created = 0
        updated = 0
        for item in attributed:
            is_new = item.slot not in known
            if not dry_run:
                await self._metrics.upsert_daily(
                    campaign_id=item.campaign_id,
                    stat_date=item.record.stat_date,
                    creative_id=item.creative_id,
                    impressions=item.record.impressions,
                    clicks=item.record.clicks,
                    conversions=item.record.conversions,
                    cost=item.record.cost,
                    revenue=item.record.revenue,
                    unique_reach=item.record.unique_reach,
                    source=source,
                    batch_id=batch.id,
                )
            if is_new:
                created += 1
            else:
                updated += 1

        batch.created = created
        batch.updated = updated
        batch.rejected = len(rejected)
        batch.unresolved = len(unresolved)
        await self._batches.flush()

        _count(
            source,
            dry_run,
            created=created,
            updated=updated,
            rejected=len(rejected),
            unresolved=len(unresolved),
        )

        await AuditService(self._session).record(
            action="metrics.ingested",
            resource_type="ingest_batch",
            resource_id=batch.id,
            claims=claims,
            after={
                "source": source,
                "actor": actor,
                "dry_run": dry_run,
                "received": received,
                "created": created,
                "updated": updated,
                "rejected": len(rejected),
                "unresolved": len(unresolved),
                "window_start": claimed_start.isoformat() if claimed_start else None,
                "window_end": claimed_end.isoformat() if claimed_end else None,
            },
            client=client,
        )

        logger.info(
            "ingest_batch_complete",
            batch_id=batch.id,
            source=source,
            dry_run=dry_run,
            received=received,
            created=created,
            updated=updated,
            rejected=len(rejected),
            unresolved=len(unresolved),
        )

        report = IngestReportOut(
            batch_id=batch.id,
            source=source,
            dry_run=dry_run,
            received=received,
            created=created,
            updated=updated,
            rejected_count=len(rejected),
            unresolved_count=len(unresolved),
            rejected=_cap(rejected),
            unresolved=_cap(unresolved),
            issues_truncated=(
                len(rejected) > MAX_ISSUES_REPORTED or len(unresolved) > MAX_ISSUES_REPORTED
            ),
            window_start=claimed_start,
            window_end=claimed_end,
        )
        report.reconciles()
        return report

    async def _attribute(
        self,
        validated: Sequence[tuple[int, MetricRecordIn]],
        unresolved: list[IngestIssue],
    ) -> list[_Attributed]:
        """Resolve records to real campaign and creative rows.

        Three lookups for the whole batch rather than three per record: an
        ingestion run is the one place where an N+1 turns into an outage, because
        N is a day of platform data across every campaign.
        """
        if not validated:
            return []

        external_pairs = sorted(
            {
                (record.platform.value, record.external_id)
                for _, record in validated
                if record.platform is not None and record.external_id
            }
        )
        external_map = await self._campaigns.ids_by_external(external_pairs)
        known_campaigns = await self._campaigns.existing_ids(
            [record.campaign_id for _, record in validated if record.campaign_id]
        )
        creative_owners = await self._creatives.campaign_by_id(
            [record.creative_id for _, record in validated if record.creative_id]
        )

        attributed: list[_Attributed] = []
        for index, record in validated:
            campaign_id, problem = _resolve_campaign(
                record, known_campaigns=known_campaigns, external_map=external_map
            )
            if campaign_id is None or problem is not None:
                unresolved.append(_issue(index, record, problem or "no campaign could be resolved"))
                continue

            creative_id = record.creative_id
            if creative_id is not None:
                owner = creative_owners.get(creative_id)
                if owner is None:
                    unresolved.append(_issue(index, record, "no creative has id " + creative_id))
                    continue
                if owner != campaign_id:
                    unresolved.append(
                        _issue(
                            index,
                            record,
                            "creative "
                            + creative_id
                            + " belongs to campaign "
                            + owner
                            + ", not "
                            + campaign_id,
                        )
                    )
                    continue

            attributed.append(
                _Attributed(
                    index=index,
                    record=record,
                    campaign_id=campaign_id,
                    creative_id=creative_id,
                )
            )
        return attributed


def _resolve_campaign(
    record: MetricRecordIn,
    *,
    known_campaigns: set[str],
    external_map: dict[tuple[str, str], str],
) -> tuple[str | None, str | None]:
    """Return the campaign id a record points at, or the reason it does not.

    When a record carries both an internal id and platform coordinates they must
    agree. Silently preferring one would hide exactly the bug worth catching: a
    producer whose mapping has drifted, writing spend against the wrong campaign.
    """
    problems: list[str] = []
    candidates: list[str] = []

    if record.campaign_id:
        if record.campaign_id in known_campaigns:
            candidates.append(record.campaign_id)
        else:
            problems.append("no campaign has id " + record.campaign_id)

    if record.platform is not None and record.external_id:
        mapped = external_map.get((record.platform.value, record.external_id))
        if mapped is not None:
            candidates.append(mapped)
        else:
            problems.append(
                "no campaign matches " + record.platform.value + "/" + record.external_id
            )

    if problems:
        return None, "; ".join(problems)

    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        return None, (
            "record addresses two different campaigns ("
            + " and ".join(distinct)
            + "); the internal id and the platform coordinates disagree"
        )
    if not distinct:
        return None, "record could not be resolved to a campaign"
    return distinct[0], None


def _semantic_problem(record: MetricRecordIn) -> str | None:
    """Value-level problems with one record, or None when it is well formed.

    These are the errors that must not abort a batch. Type errors are already
    gone - Pydantic rejected the whole request - so what is left is a row that is
    shaped correctly and says something impossible.
    """
    addressed = bool(record.campaign_id) or (
        record.platform is not None and bool(record.external_id)
    )
    if not addressed:
        return (
            "record carries neither campaign_id nor platform+external_id, "
            "so it cannot be attributed to a campaign"
        )

    negative = [
        name
        for name in DAILY_MEASUREMENTS
        if (value := getattr(record, name)) is not None and value < 0
    ]
    if negative:
        return "negative value for " + ", ".join(negative)

    # Only decidable when one record asserts both sides of the funnel. Several
    # feeds may write different columns of the same slot, so an inversion can also
    # appear in storage without any single record having been invalid; that case
    # is reconciled on read by ``domain.kpi.reconcile_delivery``.
    if (
        record.impressions is not None
        and record.clicks is not None
        and record.clicks > record.impressions
    ):
        return "clicks exceed impressions within one record"
    if (
        record.clicks is not None
        and record.conversions is not None
        and record.conversions > record.clicks
    ):
        return "conversions exceed clicks within one record"

    if not record.measurements_present():
        return "record asserts no measurements; every column is absent"

    horizon = utc_today() + timedelta(days=MAX_FUTURE_DAYS)
    if record.stat_date > horizon:
        return (
            "stat_date "
            + record.stat_date.isoformat()
            + " is more than "
            + str(MAX_FUTURE_DAYS)
            + " day(s) ahead of today"
        )
    return None


def _reject_duplicate_slots(
    attributed: Sequence[_Attributed], rejected: list[IngestIssue]
) -> list[_Attributed]:
    """Refuse to write a slot twice from one batch.

    Last-write-wins would be a guess about producer intent, and whichever record
    lost would vanish without a trace. Every record in a collision is reported
    instead, so the fix lands in the feed rather than in a support thread.
    """
    by_slot: dict[tuple[str, str | None, date], list[int]] = {}
    for item in attributed:
        by_slot.setdefault(item.slot, []).append(item.index)

    collisions = {slot: indexes for slot, indexes in by_slot.items() if len(indexes) > 1}
    if not collisions:
        return list(attributed)

    kept: list[_Attributed] = []
    for item in attributed:
        indexes = collisions.get(item.slot)
        if indexes is None:
            kept.append(item)
            continue
        others = ", ".join("#" + str(other) for other in indexes if other != item.index)
        rejected.append(
            _issue(
                item.index,
                item.record,
                "this batch asserts the same campaign/creative/day as record "
                + others
                + "; refusing to guess which is right",
            )
        )
    return kept


def _issue(index: int, record: MetricRecordIn, reason: str) -> IngestIssue:
    return IngestIssue(
        index=index, reason=reason, identity=record.identity(), stat_date=record.stat_date
    )


def _cap(issues: Sequence[IngestIssue]) -> list[IngestIssue]:
    """Keep the itemised list bounded; the counts stay exact."""
    ordered = sorted(issues, key=lambda issue: issue.index)
    return list(ordered[:MAX_ISSUES_REPORTED])


def _count(
    source: str,
    dry_run: bool,
    *,
    created: int,
    updated: int,
    rejected: int,
    unresolved: int,
) -> None:
    """Report dispositions to Prometheus.

    Labels are a fixed set of eight per source, so a misnamed feed cannot grow
    the cardinality of the series beyond what the registry already allows.
    """
    prefix = _DRY_PREFIX if dry_run else ""
    for outcome, amount in (
        ("created", created),
        ("updated", updated),
        ("rejected", rejected),
        ("unresolved", unresolved),
    ):
        INGEST_RECORDS_TOTAL.labels(source=source, outcome=prefix + outcome).inc(amount)
    INGEST_BATCHES_TOTAL.labels(source=source, mode="dry" if dry_run else "write").inc()
