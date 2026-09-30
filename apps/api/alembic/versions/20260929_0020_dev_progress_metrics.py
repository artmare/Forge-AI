"""add Dev Mode progress and context efficiency metrics

Revision ID: 20260929_0020
Revises: 20260919_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260929_0020"
down_revision: str | None = "20260919_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in (
        "reused_observations",
        "stale_observation_invalidations",
        "malformed_tool_repairs",
        "stagnation_signals",
    ):
        op.add_column(
            "task_runtime_metrics",
            sa.Column(name, sa.Integer(), server_default="0", nullable=False),
        )
    for name in ("context_component_bytes", "last_useful_action"):
        op.add_column(
            "task_runtime_metrics",
            sa.Column(
                name,
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("task_runtime_metrics", "last_useful_action")
    op.drop_column("task_runtime_metrics", "context_component_bytes")
    op.drop_column("task_runtime_metrics", "stagnation_signals")
    op.drop_column("task_runtime_metrics", "malformed_tool_repairs")
    op.drop_column("task_runtime_metrics", "stale_observation_invalidations")
    op.drop_column("task_runtime_metrics", "reused_observations")
