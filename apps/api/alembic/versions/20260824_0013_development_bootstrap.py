"""Persist deterministic development bootstrap and QA classification state.

Revision ID: 20260824_0013
Revises: 20260824_0012
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0013"
down_revision: str | None = "20260824_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_development_profiles",
        sa.Column(
            "repository_initialized", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "project_development_profiles", sa.Column("repository_branch", sa.Text(), nullable=True)
    )
    op.add_column(
        "project_development_profiles",
        sa.Column(
            "initial_checkpoint_created", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "project_development_profiles",
        sa.Column("changed_files_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "project_development_profiles",
        sa.Column(
            "last_refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "project_development_profiles",
        sa.Column("bootstrap_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "project_development_profiles", sa.Column("bootstrap_error_code", sa.Text(), nullable=True)
    )
    op.add_column(
        "project_development_profiles",
        sa.Column("bootstrap_error_message", sa.Text(), nullable=True),
    )
    op.add_column("qa_results", sa.Column("failure_classification", sa.Text(), nullable=True))
    op.add_column("qa_results", sa.Column("failure_code", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("qa_results", "failure_code")
    op.drop_column("qa_results", "failure_classification")
    for column in (
        "bootstrap_error_message",
        "bootstrap_error_code",
        "bootstrap_attempts",
        "last_refreshed_at",
        "changed_files_count",
        "initial_checkpoint_created",
        "repository_branch",
        "repository_initialized",
    ):
        op.drop_column("project_development_profiles", column)
