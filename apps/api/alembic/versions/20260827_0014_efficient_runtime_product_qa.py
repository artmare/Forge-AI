"""Add efficient runtime budgets, context caches, project knowledge, and Product QA.

Revision ID: 20260827_0014
Revises: 20260824_0013
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_0014"
down_revision: str | None = "20260824_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runtime_budgets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("max_model_calls", sa.Integer(), nullable=False),
        sa.Column("max_calls_per_iteration", sa.Integer(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("max_estimated_cost", sa.Numeric(18, 8), nullable=True),
        sa.Column("consumed_model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_cached_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_estimated_cost", sa.Numeric(18, 8), server_default="0", nullable=False),
        sa.Column("current_model_alias", sa.Text(), nullable=True),
        sa.Column("warning_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stopped_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "max_model_calls > 0", name=op.f("ck_task_runtime_budgets_max_model_calls_positive")
        ),
        sa.CheckConstraint(
            "max_calls_per_iteration > 0",
            name=op.f("ck_task_runtime_budgets_max_calls_per_iteration_positive"),
        ),
        sa.CheckConstraint(
            "max_input_tokens > 0", name=op.f("ck_task_runtime_budgets_max_input_tokens_positive")
        ),
        sa.CheckConstraint(
            "max_output_tokens > 0", name=op.f("ck_task_runtime_budgets_max_output_tokens_positive")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_task_runtime_budgets_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runtime_budgets")),
        sa.UniqueConstraint("task_id", name="uq_task_runtime_budgets_task_id"),
    )
    op.create_table(
        "model_escalations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_alias", sa.Text(), nullable=False),
        sa.Column("to_alias", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "objective_signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_model_escalations_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="SET NULL",
            name=op.f("fk_model_escalations_task_run_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_escalations")),
    )
    op.create_index(
        "ix_model_escalations_task_id_created_at", "model_escalations", ["task_id", "created_at"]
    )
    op.create_table(
        "task_runtime_metrics",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("context_bytes_sent", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "estimated_unchanged_bytes_avoided", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("repeated_reads_avoided", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_turns_detected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deterministic_executions", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "tool_signature_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_task_runtime_metrics_task_id_tasks"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runtime_metrics")),
        sa.UniqueConstraint("task_id", name="uq_task_runtime_metrics_task_id"),
    )
    op.create_table(
        "file_context_cache",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("last_model_visible_hash", sa.Text(), nullable=True),
        sa.Column("safe_summary", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name=op.f("fk_file_context_cache_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_context_cache")),
        sa.UniqueConstraint("project_id", "path", name="uq_file_context_cache_project_path"),
    )
    op.create_index(
        "ix_file_context_cache_project_id_updated_at",
        "file_context_cache",
        ["project_id", "updated_at"],
    )
    op.create_table(
        "project_knowledge_indexes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("architecture_summary", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "modules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "contracts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "decisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "recent_changes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name=op.f("fk_project_knowledge_indexes_project_id_projects"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_knowledge_indexes")),
        sa.UniqueConstraint("project_id", name="uq_project_knowledge_indexes_project_id"),
    )
    op.create_table(
        "product_qa_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "viewport_contract",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "screenshot_references",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
            name=op.f("fk_product_qa_results_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
            name=op.f("fk_product_qa_results_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_product_qa_results_task_run_id_task_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_qa_results")),
        sa.UniqueConstraint("task_id", "iteration", name="uq_product_qa_results_task_iteration"),
    )
    op.create_index(
        "ix_product_qa_results_task_id_created_at", "product_qa_results", ["task_id", "created_at"]
    )


def downgrade() -> None:
    for table in (
        "product_qa_results",
        "project_knowledge_indexes",
        "file_context_cache",
        "task_runtime_metrics",
        "model_escalations",
        "task_runtime_budgets",
    ):
        op.drop_table(table)
