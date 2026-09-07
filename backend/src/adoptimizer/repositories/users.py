"""User and refresh-session persistence."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import as_utc, utcnow
from ..core.ids import new_id
from ..core.security import Role
from ..infra.db.models import RefreshSession, User
from .base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User
    resource_name = "user"

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.sessions = RefreshSessionRepository(session)

    async def by_email(self, email: str) -> User | None:
        """Look up a user by normalised email."""
        statement = select(User).where(User.email == email.strip().lower())
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        hashed_password: str,
        role: Role = Role.VIEWER,
        full_name: str = "",
        must_change_password: bool = False,
    ) -> User:
        """Insert a new user."""
        user = User(
            id=new_id("usr"),
            email=email.strip().lower(),
            full_name=full_name,
            hashed_password=hashed_password,
            role=role.value,
            is_active=True,
            must_change_password=must_change_password,
        )
        return await self.add(user)

    async def register_login_failure(
        self, user: User, *, max_failures: int, lockout_seconds: int
    ) -> bool:
        """Increment the failure counter; returns True when the account locked."""
        user.failed_login_count += 1
        locked = False
        if user.failed_login_count >= max_failures:
            user.locked_until = utcnow() + timedelta(seconds=lockout_seconds)
            user.failed_login_count = 0
            locked = True
        await self.flush()
        return locked

    async def register_login_success(self, user: User, *, new_hash: str | None = None) -> None:
        """Reset the failure counter and record the login time."""
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        if new_hash:
            user.hashed_password = new_hash
        await self.flush()

    def is_locked(self, user: User) -> bool:
        """Whether the account is currently locked out."""
        return user.locked_until is not None and as_utc(user.locked_until) > utcnow()


class RefreshSessionRepository(BaseRepository[RefreshSession]):
    model = RefreshSession
    resource_name = "refresh_session"

    async def create(
        self,
        *,
        user_id: str,
        session_id: str,
        ttl: timedelta,
        user_agent: str = "",
        ip_address: str = "",
    ) -> RefreshSession:
        """Persist a refresh token so it can be revoked independently."""
        record = RefreshSession(
            id=session_id,
            user_id=user_id,
            expires_at=utcnow() + ttl,
            user_agent=user_agent[:400],
            ip_address=ip_address[:64],
        )
        return await self.add(record)

    async def get_valid(self, session_id: str) -> RefreshSession | None:
        """Return an unexpired, unrevoked session."""
        record = await self.get(session_id)
        if record is None or not record.is_valid:
            return None
        return record

    async def revoke(self, session_id: str) -> bool:
        """Revoke a single refresh token."""
        record = await self.get(session_id)
        if record is None or record.revoked_at is not None:
            return False
        record.revoked_at = utcnow()
        await self.flush()
        return True

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Sign a user out everywhere."""
        statement = select(RefreshSession).where(
            RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None)
        )
        rows = (await self.session.execute(statement)).scalars().all()
        now = utcnow()
        for row in rows:
            row.revoked_at = now
        await self.flush()
        return len(rows)
