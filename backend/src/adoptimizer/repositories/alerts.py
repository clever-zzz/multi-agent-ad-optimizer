"""Alert persistence with fingerprint-based suppression."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select

from ..core.ids import new_id
from ..domain.anomaly import Alert as DomainAlert
from ..domain.enums import AlertStatus
from ..infra.db.models import Alert
from .base import BaseRepository


class AlertRepository(BaseRepository[Alert]):
    model = Alert
    resource_name = "alert"

    async def upsert_from_detection(
        self, alert: DomainAlert, *, run_id: str | None = None
    ) -> tuple[Alert, bool]:
        """Insert a new alert or refresh an open one with the same fingerprint.

        Returns the record and whether it was newly created, so callers can
        count only genuinely new findings.
        """
        statement = select(Alert).where(
            Alert.dedup_key == alert.dedup_key,
            Alert.status.in_([AlertStatus.OPEN.value, AlertStatus.ACKNOWLEDGED.value]),
        )
        existing = (await self.session.execute(statement)).scalars().first()

        if existing is not None:
            existing.observed = alert.observed
            existing.threshold = alert.threshold
            existing.severity = alert.severity.value
            existing.message = alert.message
            existing.context = alert.context
            existing.detected_at = alert.detected_at
            existing.run_id = run_id or existing.run_id
            await self.flush()
            return existing, False

        record = Alert(
            id=new_id("alr"),
            campaign_id=alert.campaign_id,
            run_id=run_id,
            rule=alert.rule.value,
            severity=alert.severity.value,
            status=AlertStatus.OPEN.value,
            observed=alert.observed,
            threshold=alert.threshold,
            message=alert.message,
            dedup_key=alert.dedup_key,
            context=alert.context,
            detected_at=alert.detected_at,
        )
        return await self.add(record), True

    async def record_many(self, alerts: Sequence[DomainAlert], *, run_id: str | None = None) -> int:
        """Persist a batch and return how many were new."""
        created = 0
        for alert in alerts:
            _, is_new = await self.upsert_from_detection(alert, run_id=run_id)
            created += 1 if is_new else 0
        return created

    async def open_alerts(self, *, campaign_id: str | None = None, limit: int = 200) -> list[Alert]:
        """Currently unresolved alerts."""
        statement: Select[Any] = (
            select(Alert)
            .where(Alert.status != AlertStatus.RESOLVED.value)
            .order_by(Alert.detected_at.desc())
            .limit(limit)
        )
        if campaign_id:
            statement = statement.where(Alert.campaign_id == campaign_id)
        return list((await self.session.execute(statement)).scalars().all())

    async def acknowledge(self, alert_id: str, *, actor_id: str) -> Alert:
        """Mark an alert as seen by an operator."""
        alert = await self.get_or_raise(alert_id)
        alert.status = AlertStatus.ACKNOWLEDGED.value
        alert.acknowledged_by = actor_id
        alert.acknowledged_at = datetime.now(UTC)
        await self.flush()
        return alert

    async def resolve(self, alert_id: str, *, actor_id: str | None = None) -> Alert:
        """Close an alert."""
        alert = await self.get_or_raise(alert_id)
        alert.status = AlertStatus.RESOLVED.value
        alert.resolved_at = datetime.now(UTC)
        if actor_id and alert.acknowledged_by is None:
            alert.acknowledged_by = actor_id
            alert.acknowledged_at = datetime.now(UTC)
        await self.flush()
        return alert

    async def resolve_for_campaign(self, campaign_id: str, *, rule: str | None = None) -> int:
        """Auto-resolve alerts that no longer fire."""
        statement = select(Alert).where(
            Alert.campaign_id == campaign_id, Alert.status != AlertStatus.RESOLVED.value
        )
        if rule:
            statement = statement.where(Alert.rule == rule)
        rows = (await self.session.execute(statement)).scalars().all()
        now = datetime.now(UTC)
        for row in rows:
            row.status = AlertStatus.RESOLVED.value
            row.resolved_at = now
        await self.flush()
        return len(rows)

    async def counts_by_severity(self) -> dict[str, int]:
        """Open alert totals grouped by severity."""
        statement = (
            select(Alert.severity, func.count(Alert.id))
            .where(Alert.status != AlertStatus.RESOLVED.value)
            .group_by(Alert.severity)
        )
        return {str(row[0]): int(row[1]) for row in (await self.session.execute(statement)).all()}

    async def counts_by_status(self) -> dict[str, int]:
        """Alert totals grouped by lifecycle status, across the whole window."""
        statement = select(Alert.status, func.count(Alert.id)).group_by(Alert.status)
        return {str(row[0]): int(row[1]) for row in (await self.session.execute(statement)).all()}

    async def history(
        self, *, campaign_id: str | None = None, days: int = 30, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Alert volume over time for trend charts."""
        from datetime import timedelta

        cutoff = datetime.now(UTC) - timedelta(days=days)
        statement = (
            select(Alert.rule, Alert.severity, func.count(Alert.id).label("total"))
            .where(Alert.detected_at >= cutoff)
            .group_by(Alert.rule, Alert.severity)
            .limit(limit)
        )
        if campaign_id:
            statement = statement.where(Alert.campaign_id == campaign_id)
        return [
            {"rule": row.rule, "severity": row.severity, "total": int(row.total)}
            for row in (await self.session.execute(statement)).all()
        ]
