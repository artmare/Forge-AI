"""persist terminal Task propagation reasons

Revision ID: 20260902_0018
Revises: 20260828_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260902_0018"
down_revision: str | None = "20260828_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("terminal_reason", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "development_executions",
        sa.Column(
            "execution_origin",
            sa.Text(),
            server_default="MODEL_REQUESTED",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "execution_origin_valid",
        "development_executions",
        "execution_origin IN ('MODEL_REQUESTED', 'FORGE_QA')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "execution_origin_valid", "development_executions", type_="check"
    )
    op.drop_column("development_executions", "execution_origin")
    op.drop_column("tasks", "terminal_reason")
