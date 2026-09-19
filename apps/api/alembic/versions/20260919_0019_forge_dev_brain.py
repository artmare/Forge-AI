"""extend project knowledge for Forge Dev Mode brain

Revision ID: 20260919_0019
Revises: 20260902_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260919_0019"
down_revision: str | None = "20260902_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_knowledge_indexes",
        sa.Column("project_summary", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "project_knowledge_indexes",
        sa.Column(
            "state", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "project_knowledge_indexes",
        sa.Column(
            "lessons", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column(
        "project_knowledge_indexes",
        sa.Column(
            "checkpoints",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_knowledge_indexes", "checkpoints")
    op.drop_column("project_knowledge_indexes", "lessons")
    op.drop_column("project_knowledge_indexes", "state")
    op.drop_column("project_knowledge_indexes", "project_summary")
