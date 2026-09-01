"""Add the Phase 05 agent runtime schema.

Revision ID: 20260820_0005
Revises: 20260820_0004
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260820_0005"
down_revision: str | None = "20260820_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    agent_run_status = postgresql.ENUM(
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        name="agent_run_status",
        create_type=False,
    )
    agent_run_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("status", agent_run_status, server_default="CREATED", nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_alias", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column(
            "request",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("response", postgresql.JSONB(), nullable=True),
        sa.Column("provider_response_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status IN ('CREATED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL)",
            name=op.f("ck_agent_runs_completion_matches_status"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_agent_runs_input_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_agent_runs_output_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "total_tokens >= 0", name=op.f("ck_agent_runs_total_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name=op.f("ck_agent_runs_estimated_cost_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="RESTRICT",
            name=op.f("fk_agent_runs_agent_id_agents"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="RESTRICT",
            name=op.f("fk_agent_runs_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_agent_runs_task_run_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_task_run_id", "agent_runs", ["task_run_id"])
    op.create_index("ix_agent_runs_task_id_created_at", "agent_runs", ["task_id", "created_at"])
    op.create_index("ix_agent_runs_agent_id_created_at", "agent_runs", ["agent_id", "created_at"])
    op.create_index("ix_agent_runs_status_created_at", "agent_runs", ["status", "created_at"])
    op.create_index(
        "uq_agent_runs_one_running_per_task_run",
        "agent_runs",
        ["task_run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index(
        "ix_agent_runs_stale_running",
        "agent_runs",
        ["started_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_table("agent_runs")
    postgresql.ENUM(name="agent_run_status").drop(op.get_bind(), checkfirst=True)
