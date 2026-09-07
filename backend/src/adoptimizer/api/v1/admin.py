"""Administration: system status, audit trail, experiments and seeding."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from ...core.deps import ClaimsDep, ContainerDep, PaginationDep, SessionDep, require_role
from ...domain.enums import Role
from ...infra.db.models import ABTest, AuditLog, Campaign
from ...repositories.audit import ABTestRepository, AuditRepository
from ...schemas.api import (
    ABTestOut,
    AdminUserUpdateRequest,
    AuditEntryOut,
    SeedRequest,
    SeedResult,
    SystemInfo,
    UserOut,
)
from ...schemas.common import Page
from ...services.audit import AuditService
from ...services.auth import AuthService, user_to_dict
from ...services.seed import seed_database

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_ONLY = require_role(Role.ADMIN)


@router.get("/system", response_model=SystemInfo, summary="Runtime configuration")
async def system_info(container: ContainerDep, _: Any = ADMIN_ONLY) -> SystemInfo:
    """Non-sensitive runtime facts plus dependency health."""
    info = container.settings.public_dict()
    return SystemInfo(
        environment=str(info["environment"]),
        version=str(info["version"]),
        data_mode=str(info["data_mode"]),
        llm_provider=str(info["llm_provider"]),
        llm_model=str(info["llm_model"]),
        clickhouse_enabled=bool(info["clickhouse_enabled"]),
        redis_enabled=bool(info["redis_enabled"]),
        database_dialect=str(info["database_dialect"]),
        require_action_approval=bool(info["require_action_approval"]),
        orchestrator_mode=container.orchestrator.execution_mode,
        cache_backend=container.cache.backend_name,
        uptime_seconds=container.uptime_seconds,
    )


@router.get("/health", summary="Full dependency health")
async def dependency_health(container: ContainerDep, _: Any = ADMIN_ONLY) -> dict[str, Any]:
    """The same payload the readiness probe returns, without the status gate."""
    return await container.healthcheck()


@router.get("/audit", response_model=Page[AuditEntryOut], summary="Audit trail")
async def audit_trail(
    session: SessionDep,
    pagination: PaginationDep,
    action: Annotated[str | None, Query(max_length=60)] = None,
    resource_type: Annotated[str | None, Query(max_length=40)] = None,
    actor_id: Annotated[str | None, Query(max_length=32)] = None,
    _: Any = ADMIN_ONLY,
) -> Page[AuditEntryOut]:
    """Immutable record of every state-changing operation."""
    repository = AuditRepository(session)
    filters: list[Any] = []
    if action:
        filters.append(AuditLog.action == action)
    if resource_type:
        filters.append(AuditLog.resource_type == resource_type)
    if actor_id:
        filters.append(AuditLog.actor_id == actor_id)

    rows, total = await repository.list(
        filters=filters, pagination=pagination, order_by=AuditLog.created_at
    )
    return Page[AuditEntryOut](
        items=[AuditEntryOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/ab-tests", response_model=Page[ABTestOut], summary="List experiments")
async def list_ab_tests(
    session: SessionDep,
    pagination: PaginationDep,
    campaign_id: Annotated[str | None, Query(max_length=64)] = None,
    _: Any = ADMIN_ONLY,
) -> Page[ABTestOut]:
    repository = ABTestRepository(session)
    filters = [ABTest.campaign_id == campaign_id] if campaign_id else []
    rows, total = await repository.list(
        filters=filters, pagination=pagination, order_by=ABTest.created_at
    )
    return Page[ABTestOut](
        items=[ABTestOut.model_validate(row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/seed", response_model=SeedResult, summary="Load the demo dataset")
async def seed(
    payload: SeedRequest, session: SessionDep, container: ContainerDep, _: Any = ADMIN_ONLY
) -> SeedResult:
    """Populate an empty database with campaigns, creatives and 21 days of data.

    Refuses to overwrite existing campaigns unless force is set, and even then
    only inserts when the campaign table is empty, so it can never destroy data.
    """
    if payload.force:
        existing = (await session.execute(select(Campaign).limit(1))).scalars().first()
        if existing is not None:
            return SeedResult(skipped=True, created_admin=False)

    settings = container.settings.security
    result = await seed_database(
        session,
        admin_email=settings.bootstrap_admin_email,
        admin_password=settings.bootstrap_admin_password.get_secret_value(),
        security=settings,
    )
    await session.commit()
    return SeedResult(**result)


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Change a role or disable an account",
)
async def update_user(
    user_id: str,
    payload: AdminUserUpdateRequest,
    claims: ClaimsDep,
    session: SessionDep,
    container: ContainerDep,
    _: Any = ADMIN_ONLY,
) -> UserOut:
    """Administrative account maintenance.

    Disabling an account or changing its role revokes every one of its sessions,
    so a removed operator loses access immediately instead of at token expiry.
    The last active administrator cannot be disabled or demoted.
    """
    service = AuthService(session, container.settings.security)
    before = await service.get_user(user_id)
    snapshot_before = {"role": before.role, "is_active": before.is_active}

    user = before
    if payload.role is not None and payload.role.value != user.role:
        user = await service.set_role(user_id, role=payload.role)
    if payload.is_active is not None and payload.is_active != user.is_active:
        user = await service.set_active(user_id, is_active=payload.is_active)

    await AuditService(session).record(
        action="user.updated",
        resource_type="user",
        resource_id=user_id,
        claims=claims,
        before=snapshot_before,
        after={"role": user.role, "is_active": user.is_active},
    )
    await session.commit()
    return UserOut.model_validate(user_to_dict(user))


@router.post("/prune", summary="Apply retention policies")
async def prune(
    session: SessionDep,
    audit_days: Annotated[int, Query(ge=30, le=3650)] = 365,
    _: Any = ADMIN_ONLY,
) -> dict[str, int]:
    """Delete expired idempotency keys and audit rows past retention."""
    from ...repositories.audit import IdempotencyRepository

    audit_removed = await AuditRepository(session).prune(older_than_days=audit_days)
    idempotency_removed = await IdempotencyRepository(session).prune_expired()
    await session.commit()
    return {"audit_logs_removed": audit_removed, "idempotency_keys_removed": idempotency_removed}
