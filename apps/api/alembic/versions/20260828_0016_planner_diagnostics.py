"""Add durable planner provider diagnostics and retry history.

Revision ID: 20260828_0016
Revises: 20260828_0015
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260828_0016"
down_revision: str | None = "20260828_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_runs",
        sa.Column("provider_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "planning_runs",
        sa.Column(
            "retry_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "planning_runs",
        sa.Column("internal_diagnostics", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.create_check_constraint(
        "provider_attempts_nonnegative",
        "planning_runs",
        "provider_attempts >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "provider_attempts_nonnegative", "planning_runs", type_="check"
    )
    op.drop_column("planning_runs", "internal_diagnostics")
    op.drop_column("planning_runs", "retry_history")
    op.drop_column("planning_runs", "provider_attempts")
