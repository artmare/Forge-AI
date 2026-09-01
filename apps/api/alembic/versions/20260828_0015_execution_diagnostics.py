"""Add durable execution phases and normalized diagnostic evidence.

Revision ID: 20260828_0015
Revises: 20260827_0014
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260828_0015"
down_revision: str | None = "20260827_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    execution_phase = postgresql.ENUM(
        "PREPARING",
        "ENVIRONMENT_SETUP",
        "CHECKPOINTING",
        "AGENT_START",
        "EXECUTING",
        "VERIFYING",
        "QA",
        "FINALIZING",
        name="execution_phase",
    )
    execution_phase.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "execution_jobs",
        sa.Column(
            "phase",
            execution_phase,
            server_default="PREPARING",
            nullable=False,
        ),
    )
    op.add_column(
        "execution_jobs",
        sa.Column(
            "phase_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "execution_jobs",
        sa.Column("failure_evidence", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.add_column(
        "execution_jobs",
        sa.Column("internal_error", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.add_column(
        "execution_jobs",
        sa.Column(
            "retry_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    for name in ("environment_state", "checkpoint_state", "working_tree_state"):
        op.add_column(
            "execution_jobs",
            sa.Column(
                name,
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    for name in (
        "working_tree_state",
        "checkpoint_state",
        "environment_state",
        "retry_history",
        "failure_evidence",
        "internal_error",
        "phase_history",
        "phase",
    ):
        op.drop_column("execution_jobs", name)
    postgresql.ENUM(name="execution_phase").drop(op.get_bind(), checkfirst=True)
