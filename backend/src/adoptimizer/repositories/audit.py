"""Audit trail, idempotency store, spend ledger and experiments."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from ..core.clock import as_utc, utcnow
from ..core.ids import new_id
from ..infra.db.models import ABTest, AuditLog, IdempotencyRecord
from .base import BaseRepository


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog
    resource_name = "audit_log"

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        actor_id: str | None = None,
        actor_email: str = "",
        actor_role: str = "",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip_address: str = "",
        user_agent: str = "",
        request_id: str = "",
    ) -> AuditLog:
        """Append an immutable audit entry."""
        entry = AuditLog(
            id=new_id("aud"),
            actor_id=actor_id,
            actor_email=actor_email[:320],
            actor_role=actor_role[:20],
            action=action[:60],
            resource_type=resource_type[:40],
            resource_id=resource_id,
            before=before,
            after=after,
            ip_address=ip_address[:64],
            user_agent=user_agent[:400],
            request_id=request_id[:64],
        )
        return await self.add(entry, flush=False)

    async def recent(self, *, limit: int = 100, actor_id: str | None = None) -> list[AuditLog]:
        statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if actor_id:
            statement = statement.where(AuditLog.actor_id == actor_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def prune(self, *, older_than_days: int = 365) -> int:
        """Delete audit rows past the retention window."""
        cutoff = utcnow() - timedelta(days=older_than_days)
        # A DELETE returns a CursorResult at runtime; only that subtype exposes
        # rowcount, and the async stubs widen the return type to Result.
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff)),
        )
        return int(result.rowcount or 0)


class IdempotencyRepository(BaseRepository[IdempotencyRecord]):
    model = IdempotencyRecord
    resource_name = "idempotency_record"

    @staticmethod
    def fingerprint(method: str, path: str, body: bytes) -> str:
        """Hash a request so a reused key with a different body is rejected."""
        digest = hashlib.sha256()
        digest.update(method.encode())
        digest.update(path.encode())
        digest.update(body)
        return digest.hexdigest()

    async def get(self, key: str) -> IdempotencyRecord | None:
        """Fetch an unexpired idempotency record."""
        record = await self.session.get(IdempotencyRecord, key)
        if record is None:
            return None
        if as_utc(record.expires_at) < utcnow():
            await self.session.delete(record)
            await self.flush()
            return None
        return record

    async def store(
        self,
        *,
        key: str,
        actor_id: str,
        method: str,
        path: str,
        request_fingerprint: str,
        status_code: int,
        response_body: dict[str, Any],
        ttl: timedelta = timedelta(hours=24),
    ) -> IdempotencyRecord:
        """Persist the response for a completed idempotent request."""
        record = IdempotencyRecord(
            key=key,
            actor_id=actor_id,
            method=method,
            path=path,
            request_fingerprint=request_fingerprint,
            status_code=status_code,
            response_body=response_body,
            expires_at=utcnow() + ttl,
        )
        self.session.add(record)
        await self.flush()
        return record

    async def prune_expired(self) -> int:
        """Remove expired keys."""
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < utcnow())
            ),
        )
        return int(result.rowcount or 0)


class ABTestRepository(BaseRepository[ABTest]):
    model = ABTest
    resource_name = "ab_test"

    async def create(
        self,
        *,
        campaign_id: str,
        name: str,
        hypothesis: str = "",
        control_creative_id: str | None = None,
        variant_creative_id: str | None = None,
        metric: str = "ctr",
        minimum_detectable_effect: float = 0.1,
        required_sample_size: int = 0,
        traffic_split: float = 0.5,
        created_by_run_id: str | None = None,
    ) -> ABTest:
        experiment = ABTest(
            id=new_id("ab"),
            campaign_id=campaign_id,
            name=name,
            hypothesis=hypothesis,
            control_creative_id=control_creative_id,
            variant_creative_id=variant_creative_id,
            metric=metric,
            minimum_detectable_effect=minimum_detectable_effect,
            required_sample_size=required_sample_size,
            traffic_split=traffic_split,
            created_by_run_id=created_by_run_id,
        )
        return await self.add(experiment)

    async def running(self, *, campaign_id: str | None = None) -> list[ABTest]:
        statement = select(ABTest).where(ABTest.status == "running")
        if campaign_id:
            statement = statement.where(ABTest.campaign_id == campaign_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def conclude(
        self, experiment_id: str, *, winner_creative_id: str | None, result: dict[str, Any]
    ) -> ABTest:
        experiment = await self.get_or_raise(experiment_id)
        experiment.status = "concluded"
        experiment.concluded_at = utcnow()
        experiment.winner_creative_id = winner_creative_id
        experiment.result = result
        await self.flush()
        return experiment

    async def bulk_create(self, experiments: Sequence[ABTest]) -> list[ABTest]:
        return await self.add_all(experiments)
