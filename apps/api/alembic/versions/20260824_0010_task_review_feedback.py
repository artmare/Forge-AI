"""Add durable task review decisions and human feedback.

Revision ID: 20260824_0010
Revises: 20260820_0009
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0010"
down_revision: str | None = "20260820_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    decision = postgresql.ENUM(
        "APPROVED",
        "FIX_REQUESTED",
        name="task_review_decision",
        create_type=False,
    )
    decision.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "task_reviews",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=True),
        sa.Column("agent_run_id", sa.UUID(), nullable=True),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("decision", decision, nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "iteration > 0",
            name=op.f("ck_task_reviews_iteration_positive"),
        ),
        sa.CheckConstraint(
            "(decision = 'FIX_REQUESTED' AND feedback IS NOT NULL "
            "AND length(btrim(feedback)) > 0) OR "
            "(decision = 'APPROVED' AND feedback IS NULL)",
            name=op.f("ck_task_reviews_feedback_matches_decision"),
        ),
        sa.CheckConstraint(
            "feedback IS NULL OR length(feedback) <= 10000",
            name=op.f("ck_task_reviews_feedback_length_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="RESTRICT",
            name=op.f("fk_task_reviews_task_id_tasks"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_task_reviews_task_run_id_task_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_task_reviews_agent_run_id_agent_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_reviews")),
    )
    op.create_index(
        "uq_task_reviews_task_id_iteration",
        "task_reviews",
        ["task_id", "iteration"],
        unique=True,
    )
    op.create_index(
        "ix_task_reviews_task_id_created_at",
        "task_reviews",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_task_reviews_task_run_id",
        "task_reviews",
        ["task_run_id"],
    )
    op.create_index(
        "ix_task_reviews_agent_run_id",
        "task_reviews",
        ["agent_run_id"],
    )


def downgrade() -> None:
    op.drop_table("task_reviews")
    postgresql.ENUM(name="task_review_decision").drop(op.get_bind(), checkfirst=True)
