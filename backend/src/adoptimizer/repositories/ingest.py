"""Ingestion batch persistence."""

from __future__ import annotations

from datetime import date

from ..core.ids import new_id
from ..infra.db.models import IngestBatch
from .base import BaseRepository


class IngestBatchRepository(BaseRepository[IngestBatch]):
    model = IngestBatch
    resource_name = "ingest_batch"

    async def record(
        self,
        *,
        source: str,
        actor: str = "",
        received: int,
        created: int,
        updated: int,
        rejected: int,
        unresolved: int,
        dry_run: bool = False,
        window_start: date | None = None,
        window_end: date | None = None,
    ) -> IngestBatch:
        """Append one ingestion attempt.

        Recorded for dry runs too: a rehearsal that is indistinguishable from a
        feed that never arrived is the exact ambiguity this table exists to
        remove.
        """
        batch = IngestBatch(
            id=new_id("ing"),
            source=source,
            actor=actor[:64],
            received=received,
            created=created,
            updated=updated,
            rejected=rejected,
            unresolved=unresolved,
            dry_run=dry_run,
            window_start=window_start,
            window_end=window_end,
        )
        return await self.add(batch)
