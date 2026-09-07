"""Authentication and account administration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import SecuritySettings
from ..core.errors import AuthenticationError, ConflictError, ValidationFailure
from ..core.logging import get_logger
from ..core.security import PasswordHasherService, Role, TokenPair, TokenService
from ..infra.db.models import User
from ..repositories.users import UserRepository

logger = get_logger(__name__)

PASSWORD_DIGITS = set("0123456789")
PASSWORD_LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
PASSWORD_SYMBOLS = set("!@#$%^&*()-_=+[]{};:,.<>/?")


class AuthService:
    """Credential verification, token lifecycle and user administration."""

    def __init__(self, session: AsyncSession, settings: SecuritySettings) -> None:
        self._users = UserRepository(session)
        self._hasher = PasswordHasherService(settings)
        self._tokens = TokenService(settings)
        self._settings = settings

    async def bootstrap_admin(self) -> User | None:
        """Create the initial administrator if none exists.

        Runs at startup so a fresh deployment is immediately usable, and marks
        the account as requiring a password change.
        """
        existing = await self._users.by_email(self._settings.bootstrap_admin_email)
        if existing is not None:
            return None

        admin = await self._users.create(
            email=self._settings.bootstrap_admin_email,
            hashed_password=self._hasher.hash(
                self._settings.bootstrap_admin_password.get_secret_value()
            ),
            role=Role.ADMIN,
            full_name="Bootstrap Administrator",
            must_change_password=True,
        )
        logger.warning(
            "bootstrap_admin_created",
            email=admin.email,
            note="Change this password immediately",
        )
        return admin

    async def login(
        self, *, email: str, password: str, user_agent: str = "", ip_address: str = ""
    ) -> tuple[User, TokenPair]:
        """Verify credentials and issue tokens."""
        user = await self._users.by_email(email)

        if user is None:
            # Hash anyway so a missing account and a wrong password take the
            # same amount of time and cannot be told apart by timing.
            self._hasher.verify(self._dummy_hash(), password)
            raise AuthenticationError("Invalid email or password")

        if not user.is_active:
            raise AuthenticationError("This account has been deactivated")

        if self._users.is_locked(user):
            raise AuthenticationError(
                "This account is temporarily locked after repeated failed sign-ins"
            )

        if not self._hasher.verify(user.hashed_password, password):
            locked = await self._users.register_login_failure(
                user,
                max_failures=self._settings.max_failed_logins,
                lockout_seconds=self._settings.lockout_seconds,
            )
            logger.warning("login_failed", email=user.email, locked=locked)
            raise AuthenticationError("Invalid email or password")

        new_hash = (
            self._hasher.hash(password) if self._hasher.needs_rehash(user.hashed_password) else None
        )
        await self._users.register_login_success(user, new_hash=new_hash)

        pair = self._tokens.issue_pair(subject=user.id, email=user.email, role=user.role)
        await self._users.sessions.create(
            user_id=user.id,
            session_id=pair.session_id,
            ttl=timedelta(days=self._settings.refresh_token_ttl_days),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        logger.info("login_succeeded", user_id=user.id, role=user.role)
        return user, pair

    async def refresh(self, refresh_token: str) -> tuple[User, TokenPair]:
        """Rotate a refresh token, revoking the presented one."""
        claims = self._tokens.decode(refresh_token, expected_type="refresh")
        stored = await self._users.sessions.get_valid(claims.session_id)
        if stored is None:
            raise AuthenticationError("This session has been revoked or expired")

        user = await self._users.get(claims.subject)
        if user is None or not user.is_active:
            raise AuthenticationError("This account is no longer active")

        await self._users.sessions.revoke(claims.session_id)
        pair = self._tokens.issue_pair(subject=user.id, email=user.email, role=user.role)
        await self._users.sessions.create(
            user_id=user.id,
            session_id=pair.session_id,
            ttl=timedelta(days=self._settings.refresh_token_ttl_days),
        )
        return user, pair

    async def logout(self, refresh_token: str) -> bool:
        """Revoke a single session."""
        try:
            claims = self._tokens.decode(refresh_token, expected_type="refresh")
        except AuthenticationError:
            return False
        return await self._users.sessions.revoke(claims.session_id)

    async def logout_everywhere(self, user_id: str) -> int:
        """Revoke every session for a user."""
        return await self._users.sessions.revoke_all_for_user(user_id)

    async def change_password(
        self, *, user_id: str, current_password: str, new_password: str
    ) -> None:
        """Change a password and invalidate all other sessions."""
        user = await self._users.get_or_raise(user_id)
        if not self._hasher.verify(user.hashed_password, current_password):
            raise AuthenticationError("The current password is incorrect")

        self.validate_password_strength(new_password)
        if self._hasher.verify(user.hashed_password, new_password):
            raise ValidationFailure("The new password must differ from the current one")

        user.hashed_password = self._hasher.hash(new_password)
        user.must_change_password = False
        await self._users.flush()
        await self.logout_everywhere(user_id)
        logger.info("password_changed", user_id=user_id)

    def validate_password_strength(self, password: str) -> None:
        """Enforce the configured password policy."""
        minimum = self._settings.password_min_length
        problems: list[str] = []
        if len(password) < minimum:
            problems.append("must be at least " + str(minimum) + " characters")
        if not any(char in PASSWORD_DIGITS for char in password):
            problems.append("must contain a digit")
        if not any(char in PASSWORD_LETTERS for char in password):
            problems.append("must contain a letter")
        if not any(char in PASSWORD_SYMBOLS for char in password):
            problems.append("must contain a symbol")
        if problems:
            raise ValidationFailure("Password is too weak: " + ", ".join(problems))

    async def create_user(
        self, *, email: str, password: str, role: Role, full_name: str = ""
    ) -> User:
        """Administrator-created account."""
        if await self._users.by_email(email):
            raise ConflictError("A user with that email already exists")
        self.validate_password_strength(password)
        try:
            resolved_role = Role(role)
        except ValueError as exc:
            raise ValidationFailure("Unknown role: " + str(role)) from exc

        return await self._users.create(
            email=email,
            hashed_password=self._hasher.hash(password),
            role=resolved_role,
            full_name=full_name,
            must_change_password=True,
        )

    async def list_users(self) -> list[User]:
        """All accounts, for the admin panel."""
        rows, _ = await self._users.list(order_by=User.created_at)
        return rows

    async def get_user(self, user_id: str) -> User:
        """Fetch one account or raise NotFoundError."""
        return await self._users.get_or_raise(user_id)

    async def count_active_admins(self, *, excluding: str | None = None) -> int:
        """Number of administrators that can still sign in."""
        filters: list[Any] = [User.role == Role.ADMIN.value, User.is_active.is_(True)]
        if excluding:
            filters.append(User.id != excluding)
        return await self._users.count(filters=filters)

    async def set_active(self, user_id: str, *, is_active: bool) -> User:
        """Enable or disable an account.

        Deactivating the last administrator would lock everybody out of the
        control plane, so it is refused until another admin exists.
        """
        user = await self._users.get_or_raise(user_id)
        if (
            not is_active
            and user.role == Role.ADMIN.value
            and (await self.count_active_admins(excluding=user.id)) == 0
        ):
            raise ConflictError(
                "Cannot deactivate the last active administrator; "
                "promote another administrator first"
            )
        user.is_active = is_active
        if not is_active:
            await self.logout_everywhere(user_id)
        await self._users.flush()
        return user

    async def set_role(self, user_id: str, *, role: Role) -> User:
        """Change an account's role, revoking its sessions so the new
        permissions take effect immediately rather than on the next login."""
        user = await self._users.get_or_raise(user_id)
        if (
            user.role == Role.ADMIN.value
            and role is not Role.ADMIN
            and (await self.count_active_admins(excluding=user.id)) == 0
        ):
            raise ConflictError(
                "Cannot demote the last active administrator; promote another administrator first"
            )
        user.role = role.value
        await self._users.flush()
        await self.logout_everywhere(user_id)
        return user

    def _dummy_hash(self) -> str:
        """A stable throwaway hash used to equalise timing on unknown emails."""
        return "$argon2id$v=19$m=65536,t=3,p=2$Y29uc3RhbnRzYWx0Y29uc3RhbnQ$" + "A" * 43


def user_to_dict(user: User) -> dict[str, Any]:
    """Serialise a user for API responses, never including the hash."""
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
