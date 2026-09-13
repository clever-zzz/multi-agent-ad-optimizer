"""Critic findings, and the spend direction on an action

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13 00:00:00.000000+00:00

The critic suppresses proposals rather than deleting them, but until now that
record only ever existed in the run's in-memory state. The graph state is not
persisted, so once the process exited the withholding survived as a single
integer in the run summary and nothing else: no reason, no proposal, and no row
an operator could approve instead. ``critic_findings`` is the durable half of
that decision, so "mark rather than delete" holds across the persistence
boundary and an override is a thing a person can actually perform.

``optimization_actions`` gains ``direction`` and ``basis``. Both are nullable:
a pause or a creative refresh does not move spend, and rows written before this
revision genuinely have no direction, so backfilling one would invent a fact.
They are what let the critic tell an opposing pair (raise the bid, halve the
budget) from a coherent one without parsing the free-text reason.

Hand-written to mirror ``adoptimizer.infra.db.models`` exactly. Index names
follow the naming convention in ``infra/db/base.py``. Confirm no drift with::

    alembic upgrade head
    alembic check
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("optimization_actions", sa.Column("direction", sa.String(length=10), nullable=True))
    op.add_column("optimization_actions", sa.Column("basis", sa.JSON(), nullable=True))

    op.create_table(
        "critic_findings",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("creative_id", sa.String(length=40), nullable=True),
        sa.Column("kept_action_id", sa.String(length=40), nullable=True),
        sa.Column("kept_action_type", sa.String(length=30), nullable=False),
        sa.Column("kept_confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("suppressed_action_ids", sa.JSON(), nullable=False),
        sa.Column("suppressed_actions", sa.JSON(), nullable=False),
        sa.Column("escalate", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["optimization_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_critic_findings_run_id", "critic_findings", ["run_id"], unique=False)
    op.create_index("ix_critic_findings_kind", "critic_findings", ["kind"], unique=False)
    op.create_index("ix_critic_findings_campaign_id", "critic_findings", ["campaign_id"], unique=False)
    op.create_index("ix_finding_run_kind", "critic_findings", ["run_id", "kind"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_finding_run_kind", table_name="critic_findings")
    op.drop_index("ix_critic_findings_campaign_id", table_name="critic_findings")
    op.drop_index("ix_critic_findings_kind", table_name="critic_findings")
    op.drop_index("ix_critic_findings_run_id", table_name="critic_findings")
    op.drop_table("critic_findings")
    op.drop_column("optimization_actions", "basis")
    op.drop_column("optimization_actions", "direction")
