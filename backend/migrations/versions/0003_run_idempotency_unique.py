"""Unique index on the run idempotency key

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09 00:00:00.000000+00:00

``optimization_runs.idempotency_key`` was indexed but not unique, and
``RunRepository.find_by_idempotency_key`` is a read-then-insert. Two concurrent
requests carrying the same key could both miss the lookup and both create a run,
which is exactly the double execution the header exists to prevent. The index
makes the insert the arbiter instead of the caller.

A unique *index* rather than a unique constraint: SQLite cannot ADD CONSTRAINT,
so a constraint would need batch mode (a full table rebuild), whereas
CREATE UNIQUE INDEX is native on both SQLite and PostgreSQL.

Keyless runs are unaffected - NULLs are distinct in SQL, so the index never
fires for them, and ``start_run`` refuses a key that has no actor to scope it.

Hand-written to mirror ``adoptimizer.infra.db.models.OptimizationRun`` exactly.
Confirm no drift with::

    alembic upgrade head
    alembic check
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_run_idempotency",
        "optimization_runs",
        ["idempotency_key", "requested_by"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_run_idempotency", table_name="optimization_runs")
