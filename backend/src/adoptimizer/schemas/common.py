"""Shared API envelopes."""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class PaginationParams(BaseModel):
    """Cursor-free offset pagination with hard bounds."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(default=1, ge=1, le=100_000)
    page_size: int = Field(default=50, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page(BaseModel, Generic[T]):
    """Uniform list envelope so clients can paginate every collection alike."""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50

    @property
    def page_count(self) -> int:
        if self.page_size <= 0:
            return 0
        return -(-self.total // self.page_size)

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count


class HealthResponse(BaseModel):
    """Liveness and readiness payload."""

    status: str
    version: str
    environment: str
    uptime_seconds: float
    dependencies: dict[str, dict[str, object]] = Field(default_factory=dict)


class OkResponse(BaseModel):
    """Minimal acknowledgement envelope."""

    ok: bool = True
    detail: str = ""


def pagination_query(
    page: int = Query(default=1, ge=1, description="1-based page number"),
    page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
) -> PaginationParams:
    """FastAPI dependency producing validated pagination parameters."""
    return PaginationParams(page=page, page_size=page_size)
