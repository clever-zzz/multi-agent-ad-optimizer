"""Async engine and session lifecycle management."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from ...core.config import DatabaseSettings, get_settings
from ...core.errors import DependencyUnavailableError
from ...core.logging import get_logger
from .base import Base

logger = get_logger(__name__)

# ``Session.info`` key under which a session carries its queued hooks. Lives on
# the session rather than in a module-level registry so it cannot outlive the
# transaction it belongs to, and so concurrent requests never share a queue.
AFTER_COMMIT_HOOKS = "adoptimizer.after_commit_hooks"


def after_commit(session: AsyncSession, hook: Callable[[], None]) -> None:
    """Queue ``hook`` to run only if this session's transaction really commits.

    Anything reporting what landed in the database has to be told *after* the
    commit. Counting beforehand is the natural way to write it - the values are
    already on hand, right where the row is built - and it is wrong in the one
    case that matters. ``add_actions`` used to increment
    ``optimization_actions_total`` as it staged the rows, so a run that died on a
    constraint or a dropped connection still reported the proposals it never
    wrote. Nothing downstream can detect that: the series carries no run id to
    reconcile against, so the counter is simply too high by an amount nobody can
    recover afterwards, and it is the number an operator compares against the
    actions table.

    Hooks are drained by the ``after_commit`` listener below, which fires no
    matter who called ``commit()`` - the unit of work, a route handler or a CLI
    command. A rollback drops the queue instead.
    """
    hooks: list[Callable[[], None]] = session.info.setdefault(AFTER_COMMIT_HOOKS, [])
    hooks.append(hook)


@event.listens_for(Session, "after_commit")
def _run_after_commit_hooks(session: Session) -> None:
    """Drain what ``after_commit`` queued, now that the rows are durable.

    Bound to the class rather than per session: a session here is created per
    unit of work, so per-session listeners would be a leak by construction, and
    a class-level one costs a single dict pop on sessions that queued nothing.
    """
    hooks = session.info.pop(AFTER_COMMIT_HOOKS, None)
    for hook in hooks or ():
        try:
            hook()
        except Exception as exc:
            # The transaction is already committed. Nothing raised here may undo
            # it, and a broken hook must not turn a successful write into a 500 -
            # so it is logged and the drain continues with the remaining hooks.
            logger.error("after_commit_hook_failed", error=str(exc))


@event.listens_for(Session, "after_rollback")
def _drop_after_commit_hooks(session: Session) -> None:
    """Discard the queue: the rows those hooks describe never landed.

    Without this a session that rolls back and is then reused would fire the
    stale hooks at its next commit, reporting writes from the abandoned attempt -
    the original bug, deferred by one transaction instead of removed.
    """
    session.info.pop(AFTER_COMMIT_HOOKS, None)


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

    def _engine_kwargs(self) -> dict[str, Any]:
        """Connection-pool arguments, split by dialect.

        Extracted from ``_create_engine`` so the shape can be asserted without
        building an engine: ``server_settings`` is a driver-specific nesting and
        getting it wrong fails silently, so it needs a test that runs without a
        live PostgreSQL.
        """
        kwargs: dict[str, Any] = {"echo": self._settings.echo, "future": True}
        if not self._settings.is_sqlite:
            kwargs.update(
                pool_size=self._settings.pool_size,
                max_overflow=self._settings.max_overflow,
                pool_recycle=self._settings.pool_recycle_seconds,
                pool_pre_ping=self._settings.pool_pre_ping,
            )
            if self._settings.statement_timeout_ms is not None:
                # asyncpg takes server-side settings as a nested dict. A bare
                # ``statement_timeout`` connect arg is accepted and then ignored,
                # which is how this knob sat configured-but-inert.
                kwargs["connect_args"] = {
                    "server_settings": {
                        "statement_timeout": str(self._settings.statement_timeout_ms)
                    }
                }
            return kwargs

        # A single shared connection pool keeps SQLite writes serialised and
        # avoids "database is locked" errors under concurrent requests.
        if ":memory:" in self._settings.url:
            from sqlalchemy.pool import StaticPool

            kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        return kwargs

    def _create_engine(self) -> AsyncEngine:
        engine = create_async_engine(self._settings.url, **self._engine_kwargs())
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
