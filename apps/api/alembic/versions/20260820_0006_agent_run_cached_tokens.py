"""Add cached token usage to agent runs.

Revision ID: 20260820_0006
Revises: 20260820_0005
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260820_0006"
down_revision: str | None = "20260820_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_cached_tokens_nonnegative"),
        "agent_runs",
        "cached_tokens >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_agent_runs_cached_tokens_nonnegative"),
        "agent_runs",
        type_="check",
    )
    op.drop_column("agent_runs", "cached_tokens")
