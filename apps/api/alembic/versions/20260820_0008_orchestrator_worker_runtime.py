"""Add the Phase 07 orchestrator and worker runtime schema.

Revision ID: 20260820_0008
Revises: 20260820_0007
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260820_0008"
down_revision: str | None = "20260820_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    job_status = postgresql.ENUM(
        "PENDING",
        "CLAIMED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        name="execution_job_status",
        create_type=False,
    )
    worker_status = postgresql.ENUM(
        "ONLINE", "DRAINING", "OFFLINE", name="worker_status", create_type=False
    )
    job_status.create(op.get_bind(), checkfirst=True)
    worker_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "runtime_controls",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_runtime_controls")),
    )
    op.execute(sa.text("INSERT INTO runtime_controls (key, enabled) VALUES ('autonomy', false)"))

    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("worker_key", sa.Text(), nullable=False),
        sa.Column("status", worker_status, server_default="ONLINE", nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("concurrency > 0", name=op.f("ck_worker_nodes_concurrency_positive")),
        sa.CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= started_at",
            name=op.f("ck_worker_nodes_stopped_after_start"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_nodes")),
    )
    op.create_index("uq_worker_nodes_worker_key", "worker_nodes", ["worker_key"], unique=True)
    op.create_index(
        "ix_worker_nodes_status_heartbeat", "worker_nodes", ["status", "last_heartbeat_at"]
    )

    task_priority = postgresql.ENUM(
        "LOW", "NORMAL", "HIGH", "CRITICAL", name="task_priority", create_type=False
    )
    op.create_table(
        "execution_jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=True),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("worker_id", sa.UUID(), nullable=True),
        sa.Column("status", job_status, server_default="PENDING", nullable=False),
        sa.Column("priority", task_priority, nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", postgresql.JSONB(), nullable=True),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_execution_jobs_attempts_nonnegative")),
        sa.CheckConstraint(
            "max_attempts > 0", name=op.f("ck_execution_jobs_max_attempts_positive")
        ),
        sa.CheckConstraint(
            "attempts <= max_attempts", name=op.f("ck_execution_jobs_attempts_within_maximum")
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_execution_jobs_completion_after_start"),
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'CLAIMED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL)",
            name=op.f("ck_execution_jobs_completion_matches_status"),
        ),
        sa.CheckConstraint(
            "status = 'PENDING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_execution_jobs_active_lease_present"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="RESTRICT",
            name=op.f("fk_execution_jobs_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_execution_jobs_task_run_id_task_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="RESTRICT",
            name=op.f("fk_execution_jobs_agent_id_agents"),
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["worker_nodes.id"],
            ondelete="SET NULL",
            name=op.f("fk_execution_jobs_worker_id_worker_nodes"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_jobs")),
    )
    op.create_index("ix_execution_jobs_task_id", "execution_jobs", ["task_id"])
    op.create_index("ix_execution_jobs_task_run_id", "execution_jobs", ["task_run_id"])
    op.create_index("ix_execution_jobs_agent_id", "execution_jobs", ["agent_id"])
    op.create_index("ix_execution_jobs_worker_id", "execution_jobs", ["worker_id"])
    op.create_index(
        "ix_execution_jobs_status_available_at", "execution_jobs", ["status", "available_at"]
    )
    op.create_index("ix_execution_jobs_lease_expires_at", "execution_jobs", ["lease_expires_at"])
    op.create_index(
        "uq_execution_jobs_one_active_per_task",
        "execution_jobs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'CLAIMED', 'RUNNING')"),
    )
    op.create_index(
        "uq_execution_jobs_one_running_per_agent",
        "execution_jobs",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CLAIMED', 'RUNNING')"),
    )


def downgrade() -> None:
    op.drop_table("execution_jobs")
    op.drop_table("worker_nodes")
    op.drop_table("runtime_controls")
    postgresql.ENUM(name="worker_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="execution_job_status").drop(op.get_bind(), checkfirst=True)
