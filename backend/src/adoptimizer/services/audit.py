"""Audit trail service."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from ..core.security import TokenClaims
from ..repositories.audit import AuditRepository

logger = get_logger(__name__)


class AuditService:
    """Records who did what, when, from where."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        claims: TokenClaims | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        client: dict[str, str] | None = None,
    ) -> None:
        """Append one audit entry. Never raises into the caller's transaction."""
        metadata = client or {}
        await self._repo.record(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=claims.subject if claims else None,
            actor_email=claims.email if claims else "system",
            actor_role=claims.role.value if claims else "system",
            before=before,
            after=after,
            ip_address=metadata.get("ip_address", ""),
            user_agent=metadata.get("user_agent", ""),
            request_id=metadata.get("request_id", ""),
        )
        logger.info(
            "audit_recorded",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor=claims.subject if claims else "system",
        )

    async def recent(self, *, limit: int = 100, actor_id: str | None = None) -> list[Any]:
        """Return recent entries for the audit UI."""
        return await self._repo.recent(limit=limit, actor_id=actor_id)

    async def prune(self, *, older_than_days: int = 365) -> int:
        """Apply the retention policy."""
        return await self._repo.prune(older_than_days=older_than_days)
