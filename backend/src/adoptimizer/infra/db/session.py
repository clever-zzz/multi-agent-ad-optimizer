"""Async engine and session lifecycle management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ...core.config import DatabaseSettings, get_settings
from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from .base import Base

logger = get_logger(__name__)


class Database:
    """Owns the engine and session factory for the process."""

    def __init__(self, settings: DatabaseSettings | None = None) -> None:
        self._settings = settings or get_settings().database
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def settings(self) -> DatabaseSettings:
        return self._settings

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                bind=self.engine,
                expire_on_commit=False,
                autoflush=False,
                class_=AsyncSession,
            )
        return self._session_factory

    def _create_engine(self) -> AsyncEngine:
        kwargs: dict[str, Any] = {"echo": self._settings.echo, "future": True}
        if not self._settings.is_sqlite:
            kwargs.update(
                pool_size=self._settings.pool_size,
                max_overflow=self._settings.max_overflow,
                pool_recycle=self._settings.pool_recycle_seconds,
                pool_pre_ping=self._settings.pool_pre_ping,
            )
        else:
            # A single shared connection pool keeps SQLite writes serialised and
            # avoids "database is locked" errors under concurrent requests.
            from sqlalchemy.pool import StaticPool

            if self._settings.url.endswith(":memory:") or ":memory:" in self._settings.url:
                kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})

        engine = create_async_engine(self._settings.url, **kwargs)
        logger.info("database_engine_created", dialect=self._settings.dialect)
        return engine

    def session(self) -> AsyncSession:
        """Create a new session. Callers own its lifecycle."""
        return self.session_factory()

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[AsyncSession]:
        """Session scope that commits on success and rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_all(self) -> None:
        """Create tables. Used by tests and first-run bootstrap, not production."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def has_table(self, name: str) -> bool:
        """Report whether a table exists in the target database."""
        async with self.engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).has_table(name)
            )

    async def stamp_head(self) -> str | None:
        """Record the newest migration as applied after a create_all bootstrap."""
        head = head_revision()
        if head is None:
            return None
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version ("
                    "version_num VARCHAR(32) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
            )
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
                {"version_num": head},
            )
        logger.info("alembic_head_stamped", revision=head)
        return head

    async def healthcheck(self) -> dict[str, Any]:
        """Verify connectivity; raises DependencyUnavailableError on failure."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return {"status": "ok", "dialect": self._settings.dialect}
        except Exception as exc:
            logger.error("database_healthcheck_failed", error=str(exc))
            raise DependencyUnavailableError("Database is unreachable") from exc

    async def dispose(self) -> None:
        """Release the connection pool."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database_engine_disposed")


MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "migrations"


def head_revision() -> str | None:
    """Newest revision in the migrations directory, or None when unavailable."""
    if not MIGRATIONS_DIR.is_dir():
        return None
    try:
        from alembic.script import ScriptDirectory

        return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    except Exception as exc:
        # A missing or unreadable script directory must never block startup;
        # the caller simply skips stamping and migrations stay authoritative.
        logger.warning("alembic_head_unavailable", path=str(MIGRATIONS_DIR), error=str(exc))
        return None


async def init_database(settings: DatabaseSettings | None = None) -> Database:
    """Build a Database, bootstrapping the schema for unmanaged SQLite.

    SQLite is a development convenience. When the file carries no
    ``alembic_version`` table nothing has claimed the schema yet, so the tables
    are created and the head revision is stamped. A later ``adoptimizer migrate``
    is then a no-op instead of failing on CREATE TABLE for objects that already
    exist. PostgreSQL is always migration-managed and is never touched here.
    """
    database = Database(settings)
    if database.settings.is_sqlite and not await database.has_table("alembic_version"):
        await database.create_all()
        await database.stamp_head()
    return database


async def session_scope(database: Database) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session."""
    async with database.unit_of_work() as session:
        yield session


async def get_session() -> AsyncIterator[AsyncSession]:
    """Placeholder dependency replaced by the app factory with a bound session.

    The unreachable ``yield`` is what makes this an async generator, so FastAPI
    accepts it as a dependency and the app factory can swap it out through
    ``dependency_overrides`` before the first request arrives.
    """
    raise DependencyUnavailableError("Database session is not configured")
    yield  # type: ignore[unreachable]  # pragma: no cover
