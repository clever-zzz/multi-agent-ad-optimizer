"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-06 00:00:00.000000+00:00

Hand-written to mirror ``adoptimizer.infra.db.models`` exactly.

Constraint names are deliberately left to the naming convention declared in
``infra/db/base.py`` instead of being spelled out here, so the DDL this revision
emits is what ``Base.metadata.create_all`` already produces for SQLite
development databases and what ``alembic revision --autogenerate`` would emit
against an empty PostgreSQL schema. After installing dependencies, confirm no
drift with::

    alembic upgrade head
    alembic check

Table creation order follows the foreign key graph; downgrade reverses it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- users ------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # --- refresh_sessions -------------------------------------------------
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"], unique=False)

    # --- campaigns --------------------------------------------------------
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("daily_budget", sa.Float(), nullable=False),
        sa.Column("total_budget", sa.Float(), nullable=False),
        sa.Column("target_cpa", sa.Float(), nullable=False),
        sa.Column("target_roas", sa.Float(), nullable=False),
        sa.Column("current_bid_cpm", sa.Float(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("target_audience", sa.Text(), nullable=False),
        sa.Column("objective", sa.String(length=60), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "external_id", name="uq_campaign_platform_external"),
    )
    op.create_index("ix_campaigns_platform", "campaigns", ["platform"], unique=False)
    op.create_index("ix_campaigns_status", "campaigns", ["status"], unique=False)
    op.create_index("ix_campaigns_external_id", "campaigns", ["external_id"], unique=False)
    op.create_index(
        "ix_campaign_status_platform", "campaigns", ["status", "platform"], unique=False
    )

    # --- creatives --------------------------------------------------------
    op.create_table(
        "creatives",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("creative_type", sa.String(length=20), nullable=False),
        sa.Column("headline", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("cta_text", sa.String(length=60), nullable=False),
        sa.Column("target_emotion", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ab_group", sa.String(length=40), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("generated_by_run_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_creatives_campaign_id", "creatives", ["campaign_id"], unique=False)
    op.create_index("ix_creatives_status", "creatives", ["status"], unique=False)
    op.create_index(
        "ix_creative_campaign_status",
        "creatives",
        ["campaign_id", "status"],
        unique=False,
    )

    # --- daily_metrics ----------------------------------------------------
    op.create_table(
        "daily_metrics",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("creative_id", sa.String(length=40), nullable=True),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("conversions", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("revenue", sa.Float(), nullable=False),
        sa.Column("unique_reach", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "creative_id", "stat_date", name="uq_daily_metric_slot"),
    )
    op.create_index("ix_daily_metrics_creative_id", "daily_metrics", ["creative_id"], unique=False)
    op.create_index(
        "ix_daily_metric_campaign_date",
        "daily_metrics",
        ["campaign_id", "stat_date"],
        unique=False,
    )
    # SQL treats NULLs as distinct, so uq_daily_metric_slot does not protect the
    # campaign-level slot (creative_id IS NULL). This partial index does, on both
    # PostgreSQL and SQLite, and stops duplicate rows from double-counting spend.
    op.create_index(
        "uq_daily_metric_campaign_slot",
        "daily_metrics",
        ["campaign_id", "stat_date"],
        unique=True,
        postgresql_where=sa.text("creative_id IS NULL"),
        sqlite_where=sa.text("creative_id IS NULL"),
    )
    # --- optimization_runs ------------------------------------------------
    op.create_table(
        "optimization_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trigger_type", sa.String(length=20), nullable=False),
        sa.Column("requested_by", sa.String(length=32), nullable=True),
        sa.Column("campaign_ids", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("llm_cost_usd", sa.Float(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_optimization_runs_status", "optimization_runs", ["status"], unique=False)
    op.create_index(
        "ix_optimization_runs_idempotency_key",
        "optimization_runs",
        ["idempotency_key"],
        unique=False,
    )

    # --- run_events -------------------------------------------------------
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=20), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["optimization_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),
    )
    op.create_index("ix_run_event_run_seq", "run_events", ["run_id", "seq"], unique=False)

    # --- optimization_actions ---------------------------------------------
    op.create_table(
        "optimization_actions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("creative_id", sa.String(length=40), nullable=True),
        sa.Column("action_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("before_value", sa.String(length=200), nullable=False),
        sa.Column("after_value", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("proposed_by", sa.String(length=20), nullable=False),
        sa.Column("approved_by", sa.String(length=32), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_reference", sa.String(length=200), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["optimization_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_optimization_actions_run_id", "optimization_actions", ["run_id"], unique=False
    )
    op.create_index(
        "ix_optimization_actions_campaign_id",
        "optimization_actions",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_optimization_actions_action_type",
        "optimization_actions",
        ["action_type"],
        unique=False,
    )
    op.create_index(
        "ix_optimization_actions_status", "optimization_actions", ["status"], unique=False
    )
    op.create_index(
        "ix_action_status_type",
        "optimization_actions",
        ["status", "action_type"],
        unique=False,
    )

    # --- alerts -----------------------------------------------------------
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("rule", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("observed", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.String(length=160), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.String(length=32), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["acknowledged_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alerts_campaign_id", "alerts", ["campaign_id"], unique=False)
    op.create_index("ix_alerts_run_id", "alerts", ["run_id"], unique=False)
    op.create_index("ix_alerts_rule", "alerts", ["rule"], unique=False)
    op.create_index("ix_alerts_status", "alerts", ["status"], unique=False)
    op.create_index("ix_alerts_dedup_key", "alerts", ["dedup_key"], unique=False)
    op.create_index("ix_alert_status_detected", "alerts", ["status", "detected_at"], unique=False)

    # --- budget_allocations -----------------------------------------------
    op.create_table(
        "budget_allocations",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("current_budget", sa.Float(), nullable=False),
        sa.Column("recommended_budget", sa.Float(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("solver", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_budget_allocations_run_id", "budget_allocations", ["run_id"], unique=False)
    op.create_index(
        "ix_budget_allocations_campaign_id",
        "budget_allocations",
        ["campaign_id"],
        unique=False,
    )

    # --- ab_tests ---------------------------------------------------------
    op.create_table(
        "ab_tests",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("campaign_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("control_creative_id", sa.String(length=40), nullable=True),
        sa.Column("variant_creative_id", sa.String(length=40), nullable=True),
        sa.Column("metric", sa.String(length=20), nullable=False),
        sa.Column("minimum_detectable_effect", sa.Float(), nullable=False),
        sa.Column("required_sample_size", sa.Integer(), nullable=False),
        sa.Column("traffic_split", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner_creative_id", sa.String(length=40), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_by_run_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ab_tests_campaign_id", "ab_tests", ["campaign_id"], unique=False)
    op.create_index("ix_ab_tests_status", "ab_tests", ["status"], unique=False)

    # --- audit_logs -------------------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=40), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=400), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"], unique=False)
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_resource_type", "audit_logs", ["resource_type"], unique=False)
    op.create_index("ix_audit_logs_resource_id", "audit_logs", ["resource_id"], unique=False)
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"], unique=False)
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)

    # --- idempotency_records ----------------------------------------------
    op.create_table(
        "idempotency_records",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=400), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_idempotency_records_actor_id", "idempotency_records", ["actor_id"], unique=False
    )
    op.create_index(
        "ix_idempotency_records_expires_at",
        "idempotency_records",
        ["expires_at"],
        unique=False,
    )

    # --- llm_spend --------------------------------------------------------
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("agent", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_spend_run_id", "llm_spend", ["run_id"], unique=False)
    op.create_index("ix_llm_spend_created_at", "llm_spend", ["created_at"], unique=False)


def downgrade() -> None:
    # Reverse of upgrade: children before parents so foreign keys never dangle.
    op.drop_index("ix_llm_spend_created_at", table_name="llm_spend")
    op.drop_index("ix_llm_spend_run_id", table_name="llm_spend")
    op.drop_table("llm_spend")

    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_actor_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")

    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_ab_tests_status", table_name="ab_tests")
    op.drop_index("ix_ab_tests_campaign_id", table_name="ab_tests")
    op.drop_table("ab_tests")

    op.drop_index("ix_budget_allocations_campaign_id", table_name="budget_allocations")
    op.drop_index("ix_budget_allocations_run_id", table_name="budget_allocations")
    op.drop_table("budget_allocations")

    op.drop_index("ix_alert_status_detected", table_name="alerts")
    op.drop_index("ix_alerts_dedup_key", table_name="alerts")
    op.drop_index("ix_alerts_status", table_name="alerts")
    op.drop_index("ix_alerts_rule", table_name="alerts")
    op.drop_index("ix_alerts_run_id", table_name="alerts")
    op.drop_index("ix_alerts_campaign_id", table_name="alerts")
    op.drop_table("alerts")

    op.drop_index("ix_action_status_type", table_name="optimization_actions")
    op.drop_index("ix_optimization_actions_status", table_name="optimization_actions")
    op.drop_index("ix_optimization_actions_action_type", table_name="optimization_actions")
    op.drop_index("ix_optimization_actions_campaign_id", table_name="optimization_actions")
    op.drop_index("ix_optimization_actions_run_id", table_name="optimization_actions")
    op.drop_table("optimization_actions")

    op.drop_index("ix_run_event_run_seq", table_name="run_events")
    op.drop_table("run_events")

    op.drop_index("ix_optimization_runs_idempotency_key", table_name="optimization_runs")
    op.drop_index("ix_optimization_runs_status", table_name="optimization_runs")
    op.drop_table("optimization_runs")

    op.drop_index("uq_daily_metric_campaign_slot", table_name="daily_metrics")
    op.drop_index("ix_daily_metric_campaign_date", table_name="daily_metrics")
    op.drop_index("ix_daily_metrics_creative_id", table_name="daily_metrics")
    op.drop_table("daily_metrics")

    op.drop_index("ix_creative_campaign_status", table_name="creatives")
    op.drop_index("ix_creatives_status", table_name="creatives")
    op.drop_index("ix_creatives_campaign_id", table_name="creatives")
    op.drop_table("creatives")

    op.drop_index("ix_campaign_status_platform", table_name="campaigns")
    op.drop_index("ix_campaigns_external_id", table_name="campaigns")
    op.drop_index("ix_campaigns_status", table_name="campaigns")
    op.drop_index("ix_campaigns_platform", table_name="campaigns")
    op.drop_table("campaigns")

    op.drop_index("ix_refresh_sessions_user_id", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
