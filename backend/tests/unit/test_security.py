"""Unit tests for RBAC, JWT handling, password hashing and the password policy.

The permission matrix here is the contract the frontend mirrors, so any change
must be made in both places - and ``TestFrontendMirror`` is what turns that
sentence from a comment into a failing test.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from adoptimizer.core.config import SecuritySettings
from adoptimizer.core.errors import (
    AuthenticationError,
    PermissionDeniedError,
    ValidationFailure,
)
from adoptimizer.core.security import (
    PasswordHasherService,
    Permission,
    Role,
    TokenService,
    permissions_for,
    require_permission,
    role_has_permission,
)
from adoptimizer.services.auth import AuthService


def security_settings(**overrides: Any) -> SecuritySettings:
    kwargs: dict[str, Any] = {
        "jwt_secret": "unit-test-secret-value-that-is-long-enough",
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8192,
        "argon2_parallelism": 1,
    }
    kwargs.update(overrides)
    return SecuritySettings(**kwargs)


class TestRoleMatrix:
    def test_admin_holds_every_permission(self) -> None:
        assert permissions_for(Role.ADMIN) == frozenset(Permission)

    def test_privileges_are_strictly_ordered(self) -> None:
        # The four human roles. INGESTOR is not on this axis: it holds fewer
        # permissions than a viewer and one of them is a write.
        assert len(permissions_for(Role.ADMIN)) > len(permissions_for(Role.OPTIMIZER))
        assert len(permissions_for(Role.OPTIMIZER)) > len(permissions_for(Role.ANALYST))
        assert len(permissions_for(Role.ANALYST)) > len(permissions_for(Role.VIEWER))

    def test_analyst_contract(self) -> None:
        assert permissions_for(Role.ANALYST) == frozenset(
            {
                Permission.CAMPAIGN_READ,
                Permission.RUN_READ,
                Permission.ALERT_READ,
                Permission.ALERT_ACK,
                Permission.METRICS_READ,
                Permission.SYSTEM_READ,
            }
        )

    def test_viewer_contract(self) -> None:
        assert permissions_for(Role.VIEWER) == frozenset(
            {
                Permission.CAMPAIGN_READ,
                Permission.RUN_READ,
                Permission.ALERT_READ,
                Permission.METRICS_READ,
            }
        )

    def test_ingestor_contract(self) -> None:
        assert permissions_for(Role.INGESTOR) == frozenset(
            {Permission.METRICS_READ, Permission.METRICS_WRITE}
        )

    def test_ingestor_is_narrow_rather_than_low(self) -> None:
        # The point of the role is blast radius, not rank: a leaked pipeline
        # credential can fabricate numbers, and cannot approve those same numbers
        # into a budget change on a real ad platform.
        assert len(permissions_for(Role.INGESTOR)) < len(permissions_for(Role.VIEWER))
        for permission in Permission:
            if permission in (Permission.METRICS_READ, Permission.METRICS_WRITE):
                continue
            assert role_has_permission(Role.INGESTOR, permission) is False, permission

    def test_writing_metrics_is_not_bundled_with_operating(self) -> None:
        # The capability moved off OPTIMIZER when INGESTOR arrived. An optimizer
        # keeps read access to the numbers it optimises, and no more.
        assert role_has_permission(Role.OPTIMIZER, Permission.METRICS_READ) is True
        assert role_has_permission(Role.OPTIMIZER, Permission.METRICS_WRITE) is False
        writers = {role for role in Role if role_has_permission(role, Permission.METRICS_WRITE)}
        assert writers == {Role.ADMIN, Role.INGESTOR}

    def test_viewer_is_read_only(self) -> None:
        for permission in (
            Permission.CAMPAIGN_WRITE,
            Permission.RUN_TRIGGER,
            Permission.ACTION_APPROVE,
            Permission.ACTION_EXECUTE,
            Permission.ALERT_ACK,
            Permission.CREATIVE_WRITE,
            Permission.USER_MANAGE,
            Permission.AUDIT_READ,
        ):
            assert role_has_permission(Role.VIEWER, permission) is False, permission

    def test_optimizer_can_operate_but_not_administer(self) -> None:
        assert role_has_permission(Role.OPTIMIZER, Permission.RUN_TRIGGER) is True
        assert role_has_permission(Role.OPTIMIZER, Permission.ACTION_APPROVE) is True
        assert role_has_permission(Role.OPTIMIZER, Permission.ACTION_EXECUTE) is True
        assert role_has_permission(Role.OPTIMIZER, Permission.USER_MANAGE) is False
        assert role_has_permission(Role.OPTIMIZER, Permission.AUDIT_READ) is False

    def test_analyst_cannot_approve_or_trigger(self) -> None:
        assert role_has_permission(Role.ANALYST, Permission.ACTION_APPROVE) is False
        assert role_has_permission(Role.ANALYST, Permission.RUN_TRIGGER) is False
        assert role_has_permission(Role.ANALYST, Permission.ALERT_ACK) is True

    def test_only_admin_reads_the_audit_trail(self) -> None:
        for role in Role:
            expected = role is Role.ADMIN
            assert role_has_permission(role, Permission.AUDIT_READ) is expected

    def test_roles_accept_plain_strings(self) -> None:
        assert role_has_permission("admin", Permission.USER_MANAGE) is True
        assert permissions_for("viewer") == permissions_for(Role.VIEWER)

    def test_unknown_role_is_denied(self) -> None:
        with pytest.raises(PermissionDeniedError, match="Unknown role"):
            permissions_for("superuser")

    def test_every_role_is_mapped(self) -> None:
        # permissions_for() indexes the mapping directly, so a Role added to the
        # enum without an entry surfaces as a KeyError during a request instead
        # of a 403 - a 500 for what is really an authorisation question.
        for role in Role:
            assert isinstance(permissions_for(role), frozenset)


FRONTEND = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src"


def _frontend_matrix() -> dict[str, frozenset[str]]:
    """Parse ``ROLE_PERMISSIONS`` out of the frontend auth store."""
    block = (FRONTEND / "stores" / "auth.ts").read_text(encoding="utf-8")
    block = block.split("const ROLE_PERMISSIONS", 1)[1].split("\n};", 1)[0]
    found = re.findall(r'(\w+):\s*("\*"|\[[^\]]*\])', block)
    return {
        role: (
            {permission.value for permission in Permission}
            if body.startswith('"')
            else set(re.findall(r'"([^"]+)"', body))
        )
        for role, body in found
    }


class TestFrontendMirror:
    """The UI hides controls the API would reject, so it keeps a copy of the matrix.

    The frontend's own test can only compare that copy against a second copy in
    the same repository, which is how the two silently diverged once already. The
    check that the copy matches *this* file has to live on this side.
    """

    @pytest.fixture(autouse=True)
    def _require_the_frontend_checkout(self) -> None:
        if not FRONTEND.is_dir():
            pytest.skip("frontend/ is not checked out alongside backend/")

    def test_the_mirrored_matrix_matches_role_permissions(self) -> None:
        mirrored = _frontend_matrix()
        assert set(mirrored) == {role.value for role in Role}
        for role in Role:
            assert mirrored[role.value] == {p.value for p in permissions_for(role)}, role.value

    def test_the_role_union_matches_the_enum(self) -> None:
        source = (FRONTEND / "lib" / "types.ts").read_text(encoding="utf-8")
        line = next(item for item in source.splitlines() if item.startswith("export type Role"))
        assert set(re.findall(r'"([^"]+)"', line)) == {role.value for role in Role}

    def test_the_permission_union_matches_the_enum(self) -> None:
        # A permission the union cannot name is a permission the mirrored matrix
        # cannot grant, and the omission is invisible: the UI simply never shows
        # the control. metrics:write was missing this way.
        source = (FRONTEND / "lib" / "types.ts").read_text(encoding="utf-8")
        block = source.split("export type Permission =", 1)[1].split(";", 1)[0]
        assert set(re.findall(r'"([^"]+)"', block)) == {item.value for item in Permission}

    def test_unknown_permission_is_denied_not_raised(self) -> None:
        assert role_has_permission(Role.ADMIN, "not:a:permission") is False

    def test_require_permission_enforces_the_matrix(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="u1", email="a@b.c", role=Role.VIEWER)
        claims = tokens.decode(pair.access_token)
        require_permission(Permission.CAMPAIGN_READ, claims)
        with pytest.raises(PermissionDeniedError, match="lacks permission"):
            require_permission(Permission.CAMPAIGN_WRITE, claims)

    def test_claims_expose_the_role_permissions(self) -> None:
        tokens = TokenService(security_settings())
        claims = tokens.decode(
            tokens.issue_pair(subject="u1", email="a@b.c", role=Role.ANALYST).access_token
        )
        assert claims.permissions == permissions_for(Role.ANALYST)
        assert claims.has_permission(Permission.ALERT_ACK) is True


class TestTokenService:
    def test_round_trip_preserves_identity(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="user_1", email="ops@x.io", role=Role.OPTIMIZER)
        claims = tokens.decode(pair.access_token)
        assert claims.subject == "user_1"
        assert claims.email == "ops@x.io"
        assert claims.role is Role.OPTIMIZER
        assert claims.token_type == "access"
        assert claims.session_id

    def test_access_and_refresh_share_a_session(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="u", email="e@x.io", role=Role.ADMIN)
        access = tokens.decode(pair.access_token)
        refresh = tokens.decode(pair.refresh_token, expected_type="refresh")
        assert access.session_id == refresh.session_id

    def test_expires_in_matches_the_configuration(self) -> None:
        tokens = TokenService(security_settings(access_token_ttl_minutes=15))
        pair = tokens.issue_pair(subject="u", email="e@x.io", role=Role.ADMIN)
        assert pair.expires_in == 15 * 60
        assert pair.token_type == "bearer"

    def test_refresh_token_cannot_be_used_as_an_access_token(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="u", email="e@x.io", role=Role.ADMIN)
        with pytest.raises(AuthenticationError, match="Expected an access token"):
            tokens.decode(pair.refresh_token)

    def test_access_token_cannot_refresh(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="u", email="e@x.io", role=Role.ADMIN)
        with pytest.raises(AuthenticationError):
            tokens.decode(pair.access_token, expected_type="refresh")

    def test_tampered_token_is_rejected(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(subject="u", email="e@x.io", role=Role.VIEWER)
        # Swap in a well-formed but unrelated HS256 signature (32 zero bytes).
        tampered = pair.access_token.rsplit(".", 1)[0] + "." + "A" * 43
        with pytest.raises(AuthenticationError, match="invalid"):
            tokens.decode(tampered)

    def test_token_signed_with_another_secret_is_rejected(self) -> None:
        tokens = TokenService(security_settings())
        attacker = TokenService(security_settings(jwt_secret="a-completely-different-signing-key"))
        forged = attacker.issue_pair(subject="u", email="e@x.io", role=Role.ADMIN)
        with pytest.raises(AuthenticationError):
            tokens.decode(forged.access_token)

    def test_expired_token_is_rejected(self) -> None:
        cfg = security_settings()
        now = datetime.now(UTC)
        payload = {
            "sub": "u",
            "email": "e@x.io",
            "role": "admin",
            "sid": "s",
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
            "iss": cfg.issuer,
            "aud": cfg.audience,
            "typ": "access",
        }
        stale = jwt.encode(payload, cfg.jwt_secret.get_secret_value(), algorithm=cfg.jwt_algorithm)
        with pytest.raises(AuthenticationError, match="expired"):
            TokenService(cfg).decode(stale)

    def test_wrong_audience_is_rejected(self) -> None:
        cfg = security_settings()
        now = datetime.now(UTC)
        payload = {
            "sub": "u",
            "email": "e@x.io",
            "role": "admin",
            "sid": "s",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": cfg.issuer,
            "aud": "some-other-service",
            "typ": "access",
        }
        token = jwt.encode(payload, cfg.jwt_secret.get_secret_value(), algorithm=cfg.jwt_algorithm)
        with pytest.raises(AuthenticationError):
            TokenService(cfg).decode(token)

    def test_unknown_role_inside_a_token_is_rejected(self) -> None:
        cfg = security_settings()
        now = datetime.now(UTC)
        payload = {
            "sub": "u",
            "email": "e@x.io",
            "role": "root",
            "sid": "s",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": cfg.issuer,
            "aud": cfg.audience,
            "typ": "access",
        }
        token = jwt.encode(payload, cfg.jwt_secret.get_secret_value(), algorithm=cfg.jwt_algorithm)
        with pytest.raises(AuthenticationError, match="unknown role"):
            TokenService(cfg).decode(token)

    def test_scopes_round_trip(self) -> None:
        tokens = TokenService(security_settings())
        pair = tokens.issue_pair(
            subject="u", email="e@x.io", role=Role.ADMIN, scopes=["campaign:read"]
        )
        assert tokens.decode(pair.access_token).scopes == ["campaign:read"]


class TestPasswordHasher:
    def test_hash_is_not_the_plaintext(self) -> None:
        hasher = PasswordHasherService(security_settings())
        digest = hasher.hash("Sup3r!Secret")
        assert digest != "Sup3r!Secret"
        assert digest.startswith("$argon2id$")

    def test_hashes_are_salted(self) -> None:
        hasher = PasswordHasherService(security_settings())
        assert hasher.hash("same!Password1") != hasher.hash("same!Password1")

    def test_verify_accepts_the_right_password(self) -> None:
        hasher = PasswordHasherService(security_settings())
        digest = hasher.hash("Sup3r!Secret")
        assert hasher.verify(digest, "Sup3r!Secret") is True

    def test_verify_rejects_a_wrong_password(self) -> None:
        hasher = PasswordHasherService(security_settings())
        assert hasher.verify(hasher.hash("Sup3r!Secret"), "wrong!Password") is False

    def test_verify_rejects_a_malformed_hash(self) -> None:
        hasher = PasswordHasherService(security_settings())
        assert hasher.verify("not-an-argon2-hash", "whatever") is False

    def test_needs_rehash_detects_outdated_parameters(self) -> None:
        weak = PasswordHasherService(
            security_settings(argon2_time_cost=1, argon2_memory_cost_kib=8192)
        )
        strong = PasswordHasherService(
            security_settings(argon2_time_cost=4, argon2_memory_cost_kib=65536)
        )
        digest = weak.hash("Sup3r!Secret")
        assert strong.needs_rehash(digest) is True
        assert weak.needs_rehash(digest) is False

    def test_needs_rehash_on_garbage_is_true(self) -> None:
        assert PasswordHasherService(security_settings()).needs_rehash("garbage") is True


class TestPasswordPolicy:
    """The same policy the frontend mirrors in lib/passwordPolicy.ts."""

    @pytest.fixture
    def service(self) -> AuthService:
        return AuthService(cast("AsyncSession", None), security_settings(password_min_length=10))

    def test_accepts_a_compliant_password(self, service: AuthService) -> None:
        service.validate_password_strength("Str0ng!Passw0rd")

    @pytest.mark.parametrize(
        ("password", "expected"),
        [
            ("Sh0rt!x", "at least 10 characters"),
            ("NoDigitsHere!", "must contain a digit"),
            ("1234567890!", "must contain a letter"),
            ("Letters12345", "must contain a symbol"),
        ],
    )
    def test_rejects_weak_passwords(
        self, service: AuthService, password: str, expected: str
    ) -> None:
        with pytest.raises(ValidationFailure, match=expected):
            service.validate_password_strength(password)

    def test_every_problem_is_reported_at_once(self, service: AuthService) -> None:
        with pytest.raises(ValidationFailure) as info:
            service.validate_password_strength("abc")
        message = str(info.value)
        assert "at least 10 characters" in message
        assert "must contain a digit" in message
        assert "must contain a symbol" in message

    def test_minimum_length_is_configurable(self) -> None:
        strict = AuthService(cast("AsyncSession", None), security_settings(password_min_length=16))
        with pytest.raises(ValidationFailure, match="at least 16"):
            strict.validate_password_strength("Str0ng!Pass")


class TestLastAdministratorLock:
    """The last-administrator guard must lock the rows it counts.

    Two administrators demoting each other concurrently would each see the other
    still active and both succeed, leaving the control plane with nobody who can
    sign in. The row lock is what serialises them, so pin that the statement asks
    for it - and that it still compiles on SQLite, where ``FOR UPDATE`` does not
    exist and must degrade to a plain read rather than fail.
    """

    def test_the_guard_statement_requests_row_locks(self) -> None:
        compiled = str(
            AuthService._active_admin_lock_statement().compile(dialect=postgresql.dialect())
        )
        assert "FOR UPDATE" in compiled

    def test_sqlite_renders_it_as_a_plain_select(self) -> None:
        compiled = str(AuthService._active_admin_lock_statement().compile(dialect=sqlite.dialect()))
        assert "FOR UPDATE" not in compiled
        assert "users.role" in compiled
        assert "users.is_active" in compiled

    def test_the_lock_covers_the_whole_active_admin_set(self) -> None:
        # Locking only the *other* administrators would lock nothing in the one
        # case that matters - a single remaining admin - so the lock statement
        # must not carry the exclusion filter.
        assert len(AuthService._active_admin_filters()) == 2
        assert len(AuthService._active_admin_filters(excluding="usr_1")) == 3
