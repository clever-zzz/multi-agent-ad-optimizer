"""Alembic environment.

Resolves the database URL from application configuration so migrations always
target the same database the service uses, and imports every model so
autogenerate can compare metadata against the live schema.

The application only ships async drivers (aiosqlite, asyncpg), so migrations run
through SQLAlchemy's async engine and hand Alembic a synchronous connection via
``run_sync``. Requiring a second, sync-only driver such as psycopg2 would mean
two code paths talking to the same schema and a dependency the runtime image
does not contain.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from adoptimizer.core.config import get_settings
from adoptimizer.infra.db import models  # noqa: F401  (registers the mappers)
from adoptimizer.infra.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
database_url = settings.database.url
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

# SQLite cannot ALTER most constraints in place, so Alembic has to rebuild the
# table. Batch mode makes the same migration scripts work on both backends.
RENDER_AS_BATCH = database_url.startswith("sqlite")


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=RENDER_AS_BATCH,
        # The application never renames tables or columns implicitly; treating a
        # rename as drop+create would silently destroy data in a review.
        compare_batch_mode="render",
    )


def run_migrations_offline() -> None:
    """Emit SQL without connecting, for review before applying."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=RENDER_AS_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    """Connect through the async driver and apply migrations in a transaction."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
