"""FastAPI dependencies.

Routers declare what they need; this module resolves it from the container.
Authentication returns typed claims so permission checks read declaratively.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.container import Container
from ..core.errors import AuthenticationError
from ..core.security import Permission, Role, TokenClaims, TokenService, require_permission
from ..repositories.users import RefreshSessionRepository
from ..schemas.common import PaginationParams, pagination_query
from .logging import bind_request_context

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")


def get_container(request: Request) -> Container:
    """Resolve the application container."""
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, Container):  # pragma: no cover - startup skipped
        raise RuntimeError("Application container is not initialised")
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_db(container: ContainerDep) -> Any:
    """Yield a transactional database session."""
    async with container.database.unit_of_work() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_db)]


async def get_current_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    container: ContainerDep,
) -> TokenClaims:
    """Authenticate the caller from a bearer token."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("A bearer token is required")

    security = container.settings.security
    claims = TokenService(security).decode(credentials.credentials)

    if security.verify_session_on_request:
        await _assert_session_is_live(container, claims.session_id)

    bind_request_context(principal=claims.subject)
    return claims


async def _assert_session_is_live(container: Container, session_id: str) -> None:
    """Reject tokens whose session was revoked by logout or by an administrator.

    Access tokens are stateless, so without this check a signed-out or disabled
    operator would keep working access until the token expired.
    """
    session = container.database.session_factory()
    try:
        if await RefreshSessionRepository(session).get_valid(session_id) is None:
            raise AuthenticationError("This session has been revoked or expired")
    finally:
        await session.close()


ClaimsDep = Annotated[TokenClaims, Depends(get_current_claims)]


class PermissionChecker:
    """Dependency factory asserting a required capability."""

    def __init__(self, permission: Permission) -> None:
        self._permission = permission

    async def __call__(self, claims: ClaimsDep) -> TokenClaims:
        require_permission(self._permission, claims)
        return claims


def require(permission: Permission) -> Any:
    """Build a dependency that enforces one permission."""
    return Depends(PermissionChecker(permission))


class RoleChecker:
    """Dependency factory asserting membership in a set of roles."""

    def __init__(self, *roles: Role) -> None:
        self._roles = set(roles)

    async def __call__(self, claims: ClaimsDep) -> TokenClaims:
        if claims.role not in self._roles:
            from ..core.errors import PermissionDeniedError

            raise PermissionDeniedError(
                "This operation requires one of: " + ", ".join(sorted(r.value for r in self._roles))
            )
        return claims


def require_role(*roles: Role) -> Any:
    """Build a dependency that enforces one of the given roles."""
    return Depends(RoleChecker(*roles))


PaginationDep = Annotated[PaginationParams, Depends(pagination_query)]


def client_metadata(request: Request) -> dict[str, str]:
    """Extract audit-relevant client metadata from the request."""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "")
    )
    return {
        "ip_address": ip[:64],
        "user_agent": request.headers.get("user-agent", "")[:400],
        "request_id": str(getattr(request.state, "request_id", "") or ""),
    }
