"""Add the controlled Developer and QA execution runtime.

Revision ID: 20260824_0011
Revises: 20260824_0010
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0011"
down_revision: str | None = "20260824_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    task_kind = enum("task_kind", "GENERAL", "RESEARCH", "DOCUMENTATION", "DEVELOPMENT", "QA")
    project_type = enum("development_project_type", "NODE", "PYTHON", "STATIC_WEB", "UNKNOWN")
    package_manager = enum("development_package_manager", "NPM", "PIP", "NONE")
    action = enum(
        "development_action",
        "NODE_INSTALL",
        "NODE_TEST",
        "NODE_BUILD",
        "NODE_LINT",
        "NODE_TYPECHECK",
        "PYTHON_INSTALL",
        "PYTHON_TEST",
        "PYTHON_LINT",
        "GIT_INIT",
        "GIT_STATUS",
        "GIT_DIFF",
        "GIT_LOG",
        "GIT_CHECKPOINT",
    )
    execution_status = enum(
        "development_execution_status",
        "REQUESTED",
        "AUTHORIZED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "TIMED_OUT",
        "DENIED",
        "CANCELLED",
    )
    verification_status = enum(
        "acceptance_verification_status", "UNVERIFIED", "PASSED", "FAILED", "NOT_APPLICABLE"
    )
    qa_decision = enum("qa_decision", "PASS", "FAIL")
    for value in (
        task_kind,
        project_type,
        package_manager,
        action,
        execution_status,
        verification_status,
        qa_decision,
    ):
        value.create(bind, checkfirst=True)

    op.add_column(
        "tasks",
        sa.Column("kind", task_kind, server_default="GENERAL", nullable=False),
    )

    op.create_table(
        "project_development_profiles",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("project_type", project_type, nullable=False),
        sa.Column("package_manager", package_manager, nullable=False),
        sa.Column("install_action", action, nullable=True),
        sa.Column("test_action", action, nullable=True),
        sa.Column("build_action", action, nullable=True),
        sa.Column("lint_action", action, nullable=True),
        sa.Column("typecheck_action", action, nullable=True),
        sa.Column("detection_source", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
    )
    op.create_table(
        "project_development_leases",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "development_executions",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=True),
        sa.Column("agent_run_id", sa.UUID(), nullable=True),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("action", action, nullable=False),
        sa.Column("status", execution_status, nullable=False),
        sa.Column("working_directory", sa.Text(), nullable=False),
        sa.Column(
            "safe_arguments",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_excerpt", sa.Text(), nullable=True),
        sa.Column("stderr_excerpt", sa.Text(), nullable=True),
        sa.Column("stdout_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stderr_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "output_truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("network_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("change_summary", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Numeric(14, 2), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0", name=op.f("ck_development_executions_timeout_positive")
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_development_executions_duration_nonnegative"),
        ),
        sa.CheckConstraint(
            "stdout_bytes >= 0 AND stderr_bytes >= 0",
            name=op.f("ck_development_executions_output_bytes_nonnegative"),
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_development_executions_task_id_created_at",
        "development_executions",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_development_executions_project_id_status",
        "development_executions",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_development_executions_agent_run_id", "development_executions", ["agent_run_id"]
    )

    op.create_table(
        "qa_results",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("verifier_agent_id", sa.UUID(), nullable=True),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("decision", qa_decision, nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "checks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "blocking_issues",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "non_blocking_issues",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_qa_results_task_id_iteration", "qa_results", ["task_id", "iteration"], unique=True
    )
    op.create_index("ix_qa_results_task_id_created_at", "qa_results", ["task_id", "created_at"])

    op.create_table(
        "acceptance_verifications",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("criterion_index", sa.Integer(), nullable=False),
        sa.Column("criterion", sa.Text(), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("status", verification_status, nullable=False),
        sa.Column("verifier", sa.Text(), nullable=False),
        sa.Column("verifier_agent_id", sa.UUID(), nullable=True),
        sa.Column("evidence_summary", sa.Text(), nullable=False),
        sa.Column(
            "execution_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_acceptance_verifications_task_iteration_criterion",
        "acceptance_verifications",
        ["task_id", "iteration", "criterion_index"],
        unique=True,
    )
    op.create_index(
        "ix_acceptance_verifications_task_id_created_at",
        "acceptance_verifications",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("acceptance_verifications")
    op.drop_table("qa_results")
    op.drop_table("development_executions")
    op.drop_table("project_development_leases")
    op.drop_table("project_development_profiles")
    op.drop_column("tasks", "kind")
    bind = op.get_bind()
    for name in (
        "qa_decision",
        "acceptance_verification_status",
        "development_execution_status",
        "development_action",
        "development_package_manager",
        "development_project_type",
        "task_kind",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
