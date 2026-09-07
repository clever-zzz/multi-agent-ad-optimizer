"""Generic repository with pagination and ordering."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotFoundError
from ..core.metrics import DB_QUERY_DURATION_SECONDS
from ..infra.db.base import Base
from ..schemas.common import PaginationParams, SortOrder

ModelT = TypeVar("ModelT", bound=Base)

MAX_PAGE_SIZE = 200


class BaseRepository(Generic[ModelT]):
    """Shared CRUD and query helpers."""

    model: type[ModelT]
    resource_name: str = "resource"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    def _timed(self) -> Any:
        return DB_QUERY_DURATION_SECONDS.labels(repository=self.resource_name).time()

    async def get(self, entity_id: str) -> ModelT | None:
        """Fetch by primary key or return None."""
        with self._timed():
            return await self._session.get(self.model, entity_id)

    async def get_or_raise(self, entity_id: str) -> ModelT:
        """Fetch by primary key or raise NotFoundError."""
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFoundError(self.resource_name + " " + entity_id + " was not found")
        return entity

    async def add(self, entity: ModelT, *, flush: bool = True) -> ModelT:
        """Insert an entity."""
        self._session.add(entity)
        if flush:
            with self._timed():
                await self._session.flush()
        return entity

    async def add_all(self, entities: Sequence[ModelT], *, flush: bool = True) -> list[ModelT]:
        """Bulk insert."""
        self._session.add_all(list(entities))
        if flush:
            with self._timed():
                await self._session.flush()
        return list(entities)

    async def delete(self, entity: ModelT) -> None:
        """Remove an entity."""
        await self._session.delete(entity)
        with self._timed():
            await self._session.flush()

    async def list(
        self,
        *,
        filters: Sequence[Any] = (),
        pagination: PaginationParams | None = None,
        order_by: Any = None,
        order: SortOrder = SortOrder.DESC,
    ) -> tuple[list[ModelT], int]:
        """Return one page of rows plus the unpaginated total."""
        statement: Select[Any] = select(self.model)
        count_statement: Select[Any] = select(func.count()).select_from(self.model)

        for condition in filters:
            statement = statement.where(condition)
            count_statement = count_statement.where(condition)

        with self._timed():
            total = int(await self._session.scalar(count_statement) or 0)

        if order_by is not None:
            statement = statement.order_by(
                order_by.desc() if order == SortOrder.DESC else order_by.asc()
            )

        if pagination is not None:
            statement = statement.offset(pagination.offset).limit(
                min(pagination.limit, MAX_PAGE_SIZE)
            )

        with self._timed():
            rows = (await self._session.execute(statement)).scalars().all()
        return list(rows), total

    async def count(self, *, filters: Sequence[Any] = ()) -> int:
        """Count rows matching the filters."""
        statement: Select[Any] = select(func.count()).select_from(self.model)
        for condition in filters:
            statement = statement.where(condition)
        with self._timed():
            return int(await self._session.scalar(statement) or 0)

    async def exists(self, *, filters: Sequence[Any]) -> bool:
        """Cheap existence check."""
        statement: Select[Any] = select(self.model).where(*filters).limit(1)
        with self._timed():
            return (await self._session.execute(statement)).first() is not None

    async def flush(self) -> None:
        """Flush pending changes without committing."""
        with self._timed():
            await self._session.flush()
