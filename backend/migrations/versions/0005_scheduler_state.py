"""Scheduler state: ingestion watermarks and single-flight leases

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-09 00:00:00.000000+00:00

Two tables that make a scheduled pull observable and safe to run from more than
one place at once.

``ingest_watermarks`` records the window each feed has actually covered. It
answers "how far behind is the data" without re-reading every metric row, and it
is what lets a second run in the same window be skipped as provably redundant.
It is deliberately not the input to the schedule: a window derived from a stored
cursor inherits that cursor's mistakes forever, so the scheduler derives the
window from the calendar and only reads this row to prove a pull would add
nothing. A dry run never advances it.

``scheduler_leases`` is the single-flight guard. ``concurrencyPolicy: Forbid``
only arbitrates between jobs spawned by one CronJob; two replicas of a resident
loop, or a loop overlapping a manual trigger, need a lock visible to all of
them, and in a deployment that may not run Redis the database is the only such
place. It is a lease rather than a row lock so that a holder dying mid-pull
cannot wedge the schedule - ``expires_at`` passes and the next tick takes over.

Both are keyed by a natural name instead of a generated id, because there is
exactly one watermark per feed and one lease per job, and that primary key is
precisely what turns "claim it if it is free" into one atomic statement.

Hand-written to mirror ``adoptimizer.infra.db.models`` exactly. Confirm no drift
with::

    alembic upgrade head
    alembic check
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_watermarks",
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("batch_id", sa.String(length=40), nullable=True),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )

    op.create_table(
        "scheduler_leases",
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("holder", sa.String(length=120), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(length=20), nullable=False),
        sa.Column("last_message", sa.String(length=300), nullable=False),
        sa.Column("last_batch_id", sa.String(length=40), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("scheduler_leases")
    op.drop_table("ingest_watermarks")
