"""Metric provenance and the ingestion batch ledger

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-09 00:00:00.000000+00:00

Two changes that belong together because they answer one question: where did
this number come from?

``daily_metrics`` gains ``source`` and ``batch_id``. Both are nullable with no
server default on purpose - rows written before ingestion existed, and
everything the demo seed produces, genuinely have no known origin, and
backfilling one would be a lie that later looks like evidence. They are
provenance rather than identity, so they stay out of ``uq_daily_metric_slot``:
a slot holds one truth, whichever feed asserted it last.

``ingest_batches`` records one ingestion attempt with its counts, dry runs
included. Without it "did yesterday's feed arrive" can only be answered by
re-reading every metric row, and a rehearsal is indistinguishable from a feed
that never came.

Hand-written to mirror ``adoptimizer.infra.db.models`` exactly. Index names
follow the naming convention in ``infra/db/base.py``. Confirm no drift with::

    alembic upgrade head
    alembic check
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("daily_metrics", sa.Column("source", sa.String(length=30), nullable=True))
    op.add_column("daily_metrics", sa.Column("batch_id", sa.String(length=40), nullable=True))
    op.create_index("ix_daily_metrics_source", "daily_metrics", ["source"], unique=False)
    op.create_index("ix_daily_metrics_batch_id", "daily_metrics", ["batch_id"], unique=False)

    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("unresolved", sa.Integer(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingest_batches_source", "ingest_batches", ["source"], unique=False)
    op.create_index("ix_ingest_batches_created_at", "ingest_batches", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ingest_batches_created_at", table_name="ingest_batches")
    op.drop_index("ix_ingest_batches_source", table_name="ingest_batches")
    op.drop_table("ingest_batches")
    op.drop_index("ix_daily_metrics_batch_id", table_name="daily_metrics")
    op.drop_index("ix_daily_metrics_source", table_name="daily_metrics")
    op.drop_column("daily_metrics", "batch_id")
    op.drop_column("daily_metrics", "source")
