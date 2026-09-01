"""Add the Phase 06 controlled tool system schema.

Revision ID: 20260820_0007
Revises: 20260820_0006
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260820_0007"
down_revision: str | None = "20260820_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tool_call_status = postgresql.ENUM(
        "REQUESTED",
        "AUTHORIZED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "DENIED",
        "CANCELLED",
        name="tool_call_status",
        create_type=False,
    )
    tool_call_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("status", tool_call_status, server_default="REQUESTED", nullable=False),
        sa.Column(
            "arguments", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("permission", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(status IN ('REQUESTED', 'AUTHORIZED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'DENIED', 'CANCELLED') "
            "AND completed_at IS NOT NULL)",
            name=op.f("ck_tool_calls_completion_matches_status"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_tool_calls_completion_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tool_calls_agent_run_id_agent_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tool_calls_task_run_id_task_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], ondelete="RESTRICT", name=op.f("fk_tool_calls_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="RESTRICT",
            name=op.f("fk_tool_calls_agent_id_agents"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_agent_run_id", "tool_calls", ["agent_run_id"])
    op.create_index("ix_tool_calls_task_run_id", "tool_calls", ["task_run_id"])
    op.create_index("ix_tool_calls_task_id_created_at", "tool_calls", ["task_id", "created_at"])
    op.create_index("ix_tool_calls_agent_id_created_at", "tool_calls", ["agent_id", "created_at"])
    op.create_index("ix_tool_calls_status_created_at", "tool_calls", ["status", "created_at"])
    op.create_index(
        "ix_tool_calls_stale_running",
        "tool_calls",
        ["started_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
    postgresql.ENUM(name="tool_call_status").drop(op.get_bind(), checkfirst=True)
