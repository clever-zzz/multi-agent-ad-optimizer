"""Tool invocation audit trail

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-08 00:00:00.000000+00:00

Adds ``tool_invocations``, one row per tool call the executor handled -
including the calls it refused. Refusals are the point of the table: they are
the durable record of an agent attempting something outside its capability
set, which is what an operator reviews when deciding whether the guardrails
are set correctly.

Hand-written to mirror ``adoptimizer.infra.db.models.ToolInvocation`` exactly.
Constraint names are left to the naming convention in ``infra/db/base.py``.
Confirm no drift with::

    alembic upgrade head
    alembic check
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("agent", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("touches_platform", sa.Boolean(), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_invocations_run_id", "tool_invocations", ["run_id"], unique=False
    )
    op.create_index("ix_tool_invocations_tool", "tool_invocations", ["tool"], unique=False)
    op.create_index(
        "ix_tool_invocations_outcome", "tool_invocations", ["outcome"], unique=False
    )
    op.create_index(
        "ix_tool_invocations_created_at", "tool_invocations", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_created_at", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_outcome", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_tool", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_id", table_name="tool_invocations")
    op.drop_table("tool_invocations")
