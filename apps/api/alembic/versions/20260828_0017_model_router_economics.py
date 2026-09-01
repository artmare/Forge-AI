"""multi-provider model router and economic accounting

Revision ID: 20260828_0017
Revises: 20260828_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260828_0017"
down_revision: str | None = "20260828_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, columns in {
        "companies": (
            ("capital_available", False),
            ("revenue_recorded", False),
            ("paid_ai_budget", False),
            ("ai_spend_recorded", False),
        ),
        "missions": (("paid_ai_budget", False), ("ai_spend_recorded", False)),
        "tasks": (("paid_ai_budget", True), ("ai_spend_recorded", False)),
    }.items():
        for name, nullable in columns:
            op.add_column(
                table,
                sa.Column(
                    name,
                    sa.Numeric(18, 8),
                    nullable=nullable,
                    server_default=None if nullable else "0",
                ),
            )
    op.create_check_constraint("company_capital_nonnegative", "companies", "capital_available >= 0")
    op.create_check_constraint("company_revenue_nonnegative", "companies", "revenue_recorded >= 0")
    op.create_check_constraint("company_ai_budget_nonnegative", "companies", "paid_ai_budget >= 0")
    op.create_check_constraint(
        "company_ai_spend_nonnegative", "companies", "ai_spend_recorded >= 0"
    )
    op.create_check_constraint("mission_ai_budget_nonnegative", "missions", "paid_ai_budget >= 0")
    op.create_check_constraint("mission_ai_spend_nonnegative", "missions", "ai_spend_recorded >= 0")
    op.create_check_constraint(
        "task_ai_budget_nonnegative", "tasks", "paid_ai_budget IS NULL OR paid_ai_budget >= 0"
    )
    op.create_check_constraint("task_ai_spend_nonnegative", "tasks", "ai_spend_recorded >= 0")

    for table in ("agent_runs", "planning_runs"):
        op.add_column(table, sa.Column("economic_tier", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("selection_reason", sa.Text(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "required_capabilities",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "fallback_history",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    op.add_column(
        "task_runtime_budgets",
        sa.Column("max_free_model_calls", sa.Integer(), nullable=False, server_default="24"),
    )
    op.add_column(
        "task_runtime_budgets",
        sa.Column("max_paid_model_calls", sa.Integer(), nullable=False, server_default="8"),
    )
    op.create_check_constraint(
        "max_free_model_calls_positive", "task_runtime_budgets", "max_free_model_calls > 0"
    )
    op.create_check_constraint(
        "max_paid_model_calls_positive", "task_runtime_budgets", "max_paid_model_calls > 0"
    )

    op.create_table(
        "model_call_records",
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("mission_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("planning_run_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("economic_tier", sa.Text(), nullable=False),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("agent_role", sa.Text(), nullable=False),
        sa.Column("selection_reason", sa.Text(), nullable=False),
        sa.Column(
            "required_capabilities",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("fallback_from_provider", sa.Text(), nullable=True),
        sa.Column("fallback_from_model", sa.Text(), nullable=True),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column("reserved_cost", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("reserved_cost >= 0", name="model_call_reserved_cost_nonnegative"),
        sa.CheckConstraint("estimated_cost >= 0", name="model_call_estimated_cost_nonnegative"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["mission_id"], ["missions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["planning_run_id"], ["planning_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_calls_company_created", "model_call_records", ["company_id", "created_at"]
    )
    op.create_index(
        "ix_model_calls_mission_created", "model_call_records", ["mission_id", "created_at"]
    )
    op.create_index("ix_model_calls_task_created", "model_call_records", ["task_id", "created_at"])

    op.create_table(
        "model_provider_health",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="HEALTHY"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model_id", name="uq_model_provider_health_target"),
    )


def downgrade() -> None:
    op.drop_table("model_provider_health")
    op.drop_index("ix_model_calls_task_created", table_name="model_call_records")
    op.drop_index("ix_model_calls_mission_created", table_name="model_call_records")
    op.drop_index("ix_model_calls_company_created", table_name="model_call_records")
    op.drop_table("model_call_records")
    op.drop_constraint("max_paid_model_calls_positive", "task_runtime_budgets", type_="check")
    op.drop_constraint("max_free_model_calls_positive", "task_runtime_budgets", type_="check")
    op.drop_column("task_runtime_budgets", "max_paid_model_calls")
    op.drop_column("task_runtime_budgets", "max_free_model_calls")
    for table in ("planning_runs", "agent_runs"):
        op.drop_column(table, "fallback_history")
        op.drop_column(table, "required_capabilities")
        op.drop_column(table, "selection_reason")
        op.drop_column(table, "economic_tier")
    op.drop_constraint("task_ai_spend_nonnegative", "tasks", type_="check")
    op.drop_constraint("task_ai_budget_nonnegative", "tasks", type_="check")
    op.drop_constraint("mission_ai_spend_nonnegative", "missions", type_="check")
    op.drop_constraint("mission_ai_budget_nonnegative", "missions", type_="check")
    op.drop_constraint("company_ai_spend_nonnegative", "companies", type_="check")
    op.drop_constraint("company_ai_budget_nonnegative", "companies", type_="check")
    op.drop_constraint("company_revenue_nonnegative", "companies", type_="check")
    op.drop_constraint("company_capital_nonnegative", "companies", type_="check")
    op.drop_column("tasks", "ai_spend_recorded")
    op.drop_column("tasks", "paid_ai_budget")
    op.drop_column("missions", "ai_spend_recorded")
    op.drop_column("missions", "paid_ai_budget")
    op.drop_column("companies", "ai_spend_recorded")
    op.drop_column("companies", "paid_ai_budget")
    op.drop_column("companies", "revenue_recorded")
    op.drop_column("companies", "capital_available")
