"""Authentication and authorisation primitives.

Argon2id password hashing, JWT issuance/verification and a small role based
access control matrix. Roles map to permission strings so routers can assert
capabilities without hard-coding role names.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel, Field

from ..domain.enums import Permission, Role
from .config import SecuritySettings, get_settings
from .errors import AuthenticationError, PermissionDeniedError

# Role and Permission live in the domain layer; re-exported here because
# security is the module callers naturally reach for.
__all__ = [
    "PasswordHasherService",
    "Permission",
    "Role",
    "TokenClaims",
    "TokenPair",
    "TokenService",
    "permissions_for",
    "require_permission",
    "role_has_permission",
]


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.OPTIMIZER: frozenset(
        {
            Permission.CAMPAIGN_READ,
            Permission.CAMPAIGN_WRITE,
            Permission.RUN_READ,
            Permission.RUN_TRIGGER,
            Permission.ACTION_APPROVE,
            Permission.ACTION_EXECUTE,
            Permission.ALERT_READ,
            Permission.ALERT_ACK,
            Permission.CREATIVE_WRITE,
            Permission.METRICS_READ,
            Permission.SYSTEM_READ,
        }
    ),
    Role.ANALYST: frozenset(
        {
            Permission.CAMPAIGN_READ,
            Permission.RUN_READ,
            Permission.ALERT_READ,
            Permission.ALERT_ACK,
            Permission.METRICS_READ,
            Permission.SYSTEM_READ,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Permission.CAMPAIGN_READ,
            Permission.RUN_READ,
            Permission.ALERT_READ,
            Permission.METRICS_READ,
        }
    ),
    # A machine identity for the metrics pipeline and for nothing else.
    #
    # Ingestion can silently corrupt every downstream decision, so the capability
    # has to be reachable without an admin token - a cron job authenticating as
    # admin is the worse failure. It used to live on OPTIMIZER, on the argument
    # that an optimizer token already carries CAMPAIGN_WRITE and ACTION_EXECUTE so
    # granting it adds no escalation. That is true about the *role* and beside the
    # point about the *credential*: the one that gets copied into a pipeline
    # runner, a git secret store and a dozen log files. Splitting it out means a
    # leaked ingestion credential can fabricate numbers, and cannot approve its
    # own fabricated numbers into a real budget change.
    Role.INGESTOR: frozenset({Permission.METRICS_READ, Permission.METRICS_WRITE}),
}


def permissions_for(role: Role | str) -> frozenset[Permission]:
    """Return the permission set granted to a role."""
    try:
        resolved = Role(role)
    except ValueError as exc:
        msg = "Unknown role: " + str(role)
        raise PermissionDeniedError(msg) from exc
    return _ROLE_PERMISSIONS[resolved]


def role_has_permission(role: Role | str, permission: Permission | str) -> bool:
    """Check whether a role grants a specific permission."""
    try:
        return Permission(permission) in permissions_for(role)
    except ValueError:
        return False


class TokenPair(BaseModel):
    """Access and refresh tokens issued on successful authentication."""

    access_token: str
    refresh_token: str
    # OAuth 2.0 (RFC 6749) response field naming the token scheme; not a secret.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Access token lifetime in seconds")
    # The `sid` claim carried by both tokens, and the primary key of the persisted
    # refresh session. Callers must use this value rather than deriving their own,
    # otherwise the row written at login can never be found again at verification
    # time and every authenticated request is rejected.
    session_id: str = Field(description="Server-side session key shared by both tokens")


class TokenClaims(BaseModel):
    """Validated payload of an access token."""

    subject: str
    email: str
    role: Role
    session_id: str
    issued_at: datetime
    expires_at: datetime
    # Claim that tells access tokens from refresh tokens; not a credential.
    token_type: str = "access"  # noqa: S105
    scopes: list[str] = Field(default_factory=list)

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.role)

    def has_permission(self, permission: Permission | str) -> bool:
        return role_has_permission(self.role, permission)


class PasswordHasherService:
    """Argon2id hashing with parameters taken from configuration."""

    def __init__(self, settings: SecuritySettings | None = None) -> None:
        cfg = settings or get_settings().security
        self._hasher = PasswordHasher(
            time_cost=cfg.argon2_time_cost,
            memory_cost=cfg.argon2_memory_cost_kib,
            parallelism=cfg.argon2_parallelism,
        )

    def hash(self, password: str) -> str:
        """Return an Argon2id hash of the plaintext password."""
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        """Verify a plaintext password against a stored hash."""
        try:
            return self._hasher.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        """Detect hashes created with outdated parameters."""
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return True


class TokenService:
    """JWT issuance and verification."""

    def __init__(self, settings: SecuritySettings | None = None) -> None:
        self._cfg = settings or get_settings().security

    def issue_pair(
        self, *, subject: str, email: str, role: Role | str, scopes: list[str] | None = None
    ) -> TokenPair:
        """Issue a fresh access/refresh token pair."""
        now = datetime.now(UTC)
        session_id = uuid.uuid4().hex
        access_ttl = timedelta(minutes=self._cfg.access_token_ttl_minutes)
        refresh_ttl = timedelta(days=self._cfg.refresh_token_ttl_days)

        access = self._encode(
            subject=subject,
            email=email,
            role=str(role),
            session_id=session_id,
            issued_at=now,
            expires_at=now + access_ttl,
            token_type="access",  # noqa: S106
            scopes=scopes or [],
        )
        refresh = self._encode(
            subject=subject,
            email=email,
            role=str(role),
            session_id=session_id,
            issued_at=now,
            expires_at=now + refresh_ttl,
            token_type="refresh",  # noqa: S106
            scopes=[],
        )
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(access_ttl.total_seconds()),
            session_id=session_id,
        )

    def _encode(
        self,
        *,
        subject: str,
        email: str,
        role: str,
        session_id: str,
        issued_at: datetime,
        expires_at: datetime,
        token_type: str,
        scopes: list[str],
    ) -> str:
        payload: dict[str, Any] = {
            "sub": subject,
            "email": email,
            "role": role,
            "sid": session_id,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "nbf": int(issued_at.timestamp()),
            "iss": self._cfg.issuer,
            "aud": self._cfg.audience,
            "typ": token_type,
            "jti": uuid.uuid4().hex,
            "scopes": scopes,
        }
        return jwt.encode(
            payload,
            self._cfg.jwt_secret.get_secret_value(),
            algorithm=self._cfg.jwt_algorithm,
        )

    def decode(self, token: str, *, expected_type: str = "access") -> TokenClaims:
        """Verify signature, expiry, issuer, audience and token type."""
        try:
            payload = jwt.decode(
                token,
                self._cfg.jwt_secret.get_secret_value(),
                algorithms=[self._cfg.jwt_algorithm],
                issuer=self._cfg.issuer,
                audience=self._cfg.audience,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError("Token is invalid") from exc

        if payload.get("typ") != expected_type:
            article = "an" if expected_type[:1].lower() in "aeiou" else "a"
            msg = "Expected " + article + " " + expected_type + " token"
            raise AuthenticationError(msg)

        try:
            role = Role(payload.get("role", ""))
        except ValueError as exc:
            raise AuthenticationError("Token carries an unknown role") from exc

        return TokenClaims(
            subject=str(payload["sub"]),
            email=str(payload.get("email", "")),
            role=role,
            session_id=str(payload.get("sid", "")),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            token_type=str(payload.get("typ", "access")),
            scopes=[str(s) for s in payload.get("scopes", [])],
        )


def require_permission(permission: Permission, claims: TokenClaims) -> None:
    """Raise PermissionDeniedError when the principal lacks a capability."""
    if not claims.has_permission(permission):
        msg = "Role " + claims.role.value + " lacks permission " + permission.value
        raise PermissionDeniedError(msg)
