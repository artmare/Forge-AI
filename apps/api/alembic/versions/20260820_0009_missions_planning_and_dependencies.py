"""Add Phase 08 missions, planning runs, and task dependencies.

Revision ID: 20260820_0009
Revises: 20260820_0008
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260820_0009"
down_revision: str | None = "20260820_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE project_status ADD VALUE IF NOT EXISTS 'CANCELLED'")

    mission_status = postgresql.ENUM(
        "DRAFT",
        "PLANNING",
        "PLAN_READY",
        "ACTIVE",
        "REVIEW",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        name="mission_status",
        create_type=False,
    )
    planning_run_status = postgresql.ENUM(
        "CREATED",
        "RUNNING",
        "SUCCEEDED",
        "INVALID",
        "FAILED",
        "CANCELLED",
        name="planning_run_status",
        create_type=False,
    )
    mission_status.create(op.get_bind(), checkfirst=True)
    planning_run_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "missions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "context", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", mission_status, server_default="DRAFT", nullable=False),
        sa.Column("planning_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_planning_attempts", sa.Integer(), nullable=False),
        sa.Column("planning_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planning_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "planning_attempts >= 0", name=op.f("ck_missions_planning_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "max_planning_attempts > 0",
            name=op.f("ck_missions_max_planning_attempts_positive"),
        ),
        sa.CheckConstraint(
            "planning_attempts <= max_planning_attempts",
            name=op.f("ck_missions_planning_attempts_within_maximum"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="RESTRICT",
            name=op.f("fk_missions_company_id_companies"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
            name=op.f("fk_missions_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_missions")),
    )
    op.create_index("ix_missions_company_id_status", "missions", ["company_id", "status"])
    op.create_index("ix_missions_project_id", "missions", ["project_id"], unique=True)
    op.create_index("ix_missions_status_created_at", "missions", ["status", "created_at"])

    op.create_table(
        "planning_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("mission_id", sa.UUID(), nullable=False),
        sa.Column("status", planning_run_status, server_default="CREATED", nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_alias", sa.Text(), nullable=False),
        sa.Column("resolved_model", sa.Text(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=True),
        sa.Column("validation_result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_planning_runs_input_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_planning_runs_output_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "cached_tokens >= 0", name=op.f("ck_planning_runs_cached_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name=op.f("ck_planning_runs_estimated_cost_nonnegative"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_planning_runs_completion_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["missions.id"],
            ondelete="RESTRICT",
            name=op.f("fk_planning_runs_mission_id_missions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_planning_runs")),
    )
    op.create_index(
        "ix_planning_runs_mission_id_created_at",
        "planning_runs",
        ["mission_id", "created_at"],
    )
    op.create_index("ix_planning_runs_status_started_at", "planning_runs", ["status", "started_at"])
    op.create_index(
        "uq_planning_runs_one_active_per_mission",
        "planning_runs",
        ["mission_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CREATED', 'RUNNING')"),
    )

    op.create_table(
        "task_dependencies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("depends_on_task_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id", name=op.f("ck_task_dependencies_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_task_dependencies_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_task_dependencies_depends_on_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_dependencies")),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependencies_edge"),
    )
    op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    op.create_index(
        "ix_task_dependencies_depends_on_task_id",
        "task_dependencies",
        ["depends_on_task_id"],
    )


def downgrade() -> None:
    op.drop_table("task_dependencies")
    op.drop_table("planning_runs")
    op.drop_table("missions")
    postgresql.ENUM(name="planning_run_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="mission_status").drop(op.get_bind(), checkfirst=True)
    # PostgreSQL enum labels are intentionally not removed during downgrade; doing so
    # requires replacing the type and risks invalidating concurrent project rows.
