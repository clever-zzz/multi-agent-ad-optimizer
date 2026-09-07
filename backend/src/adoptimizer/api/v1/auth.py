"""Authentication and account endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...core.deps import ClaimsDep, ContainerDep, SessionDep, client_metadata, require_role
from ...core.errors import AuthenticationError
from ...domain.enums import Role
from ...schemas.api import (
    ChangePasswordRequest,
    CreateUserRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserOut,
)
from ...services.auth import AuthService, user_to_dict

router = APIRouter(prefix="/auth", tags=["auth"])


def _service(session: SessionDep, container: ContainerDep) -> AuthService:
    return AuthService(session, container.settings.security)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for tokens",
)
async def login(
    payload: LoginRequest, request: Request, session: SessionDep, container: ContainerDep
) -> TokenResponse:
    """Verify a password and issue an access/refresh token pair."""
    metadata = client_metadata(request)
    service = _service(session, container)
    try:
        user, pair = await service.login(
            email=payload.email,
            password=payload.password,
            user_agent=metadata["user_agent"],
            ip_address=metadata["ip_address"],
        )
    except AuthenticationError:
        # The failed-attempt counter has to outlive the rejection. unit_of_work
        # rolls back when a handler raises, so without this commit the lockout
        # policy would never engage however many guesses an attacker made.
        await session.commit()
        raise
    await session.commit()
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user=UserOut.model_validate(user_to_dict(user)),
    )


@router.post("/refresh", response_model=TokenResponse, summary="Rotate a refresh token")
async def refresh(
    payload: RefreshRequest, session: SessionDep, container: ContainerDep
) -> TokenResponse:
    """Issue a new pair and revoke the presented refresh token."""
    user, pair = await _service(session, container).refresh(payload.refresh_token)
    await session.commit()
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user=UserOut.model_validate(user_to_dict(user)),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke a session")
async def logout(payload: RefreshRequest, session: SessionDep, container: ContainerDep) -> None:
    """Revoke the refresh token so it cannot be reused."""
    await _service(session, container).logout(payload.refresh_token)
    await session.commit()


@router.get("/me", response_model=UserOut, summary="Current profile")
async def me(claims: ClaimsDep, session: SessionDep) -> UserOut:
    """Return the authenticated principal."""
    from ...repositories.users import UserRepository

    user = await UserRepository(session).get_or_raise(claims.subject)
    return UserOut.model_validate(user_to_dict(user))


@router.post(
    "/change-password", status_code=status.HTTP_204_NO_CONTENT, summary="Change own password"
)
async def change_password(
    payload: ChangePasswordRequest,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
) -> None:
    """Update the caller's password and revoke every other session."""
    await _service(session, container).change_password(
        user_id=claims.subject,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    await session.commit()


@router.post(
    "/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke all sessions"
)
async def logout_everywhere(
    claims: ClaimsDep, session: SessionDep, container: ContainerDep
) -> None:
    """Sign the caller out on every device."""
    await _service(session, container).logout_everywhere(claims.subject)
    await session.commit()


@router.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    dependencies=[require_role(Role.ADMIN)],
)
async def create_user(
    payload: CreateUserRequest, session: SessionDep, container: ContainerDep
) -> UserOut:
    """Administrators provision new operators."""
    user = await _service(session, container).create_user(
        email=payload.email,
        password=payload.password,
        role=payload.role,
        full_name=payload.full_name,
    )
    await session.commit()
    return UserOut.model_validate(user_to_dict(user))


@router.get(
    "/users",
    response_model=list[UserOut],
    summary="List accounts",
    dependencies=[require_role(Role.ADMIN)],
)
async def list_users(session: SessionDep, container: ContainerDep) -> list[UserOut]:
    """Every account, for the admin panel."""
    users = await _service(session, container).list_users()
    return [UserOut.model_validate(user_to_dict(user)) for user in users]
