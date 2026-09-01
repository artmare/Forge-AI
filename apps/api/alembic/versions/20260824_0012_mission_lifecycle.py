"""Add organizational Mission archival and deletion tombstones.

Revision ID: 20260824_0012
Revises: 20260824_0011
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0012"
down_revision: str | None = "20260824_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("missions", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column(
        "missions",
        sa.Column("owns_company", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "missions",
        sa.Column("owns_project", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Every materialized Project in Phase 08/09 was created by CompanyFactory for its Mission.
    # Existing Company ownership cannot be proven, so it intentionally remains false.
    op.execute("UPDATE missions SET owns_project = true WHERE project_id IS NOT NULL")
    op.create_index("ix_missions_archived_at", "missions", ["archived_at"])

    op.create_table(
        "mission_deletion_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("mission_id", sa.UUID(), nullable=False),
        sa.Column("mission_title", sa.Text(), nullable=False),
        sa.Column("company_id", sa.UUID()),
        sa.Column("project_id", sa.UUID()),
        sa.Column("company_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("project_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "workspace_cleanup_status",
            sa.Text(),
            server_default="REQUESTED",
            nullable=False,
        ),
        sa.Column("workspace_cleanup_error", sa.Text()),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mission_deletion_records")),
    )
    op.create_index(
        "ix_mission_deletion_records_mission_id",
        "mission_deletion_records",
        ["mission_id"],
        unique=True,
    )
    op.create_index(
        "ix_mission_deletion_records_cleanup_status",
        "mission_deletion_records",
        ["workspace_cleanup_status"],
    )


def downgrade() -> None:
    op.drop_table("mission_deletion_records")
    op.drop_index("ix_missions_archived_at", table_name="missions")
    op.drop_column("missions", "owns_project")
    op.drop_column("missions", "owns_company")
    op.drop_column("missions", "archived_at")
