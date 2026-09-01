"""Phase 02 persistent domain model.

Revision ID: 20260819_0001
Revises:
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260819_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

company_status = postgresql.ENUM(
    "CREATED", "ACTIVE", "PAUSED", "COMPLETED", "FAILED", "ARCHIVED", name="company_status"
)
project_status = postgresql.ENUM(
    "CREATED",
    "PLANNING",
    "ACTIVE",
    "REVIEW",
    "COMPLETED",
    "FAILED",
    "ARCHIVED",
    name="project_status",
)
agent_status = postgresql.ENUM(
    "CREATED", "IDLE", "BUSY", "PAUSED", "STOPPED", "FAILED", name="agent_status"
)
task_status = postgresql.ENUM(
    "CREATED",
    "QUEUED",
    "IN_PROGRESS",
    "REVIEW",
    "FIX_REQUIRED",
    "DONE",
    "FAILED",
    "CANCELLED",
    name="task_status",
)
task_priority = postgresql.ENUM("LOW", "NORMAL", "HIGH", "CRITICAL", name="task_priority")
task_run_status = postgresql.ENUM(
    "STARTED", "SUCCEEDED", "FAILED", "CANCELLED", name="task_run_status"
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        company_status,
        project_status,
        agent_status,
        task_status,
        task_priority,
        task_run_status,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "companies",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="company_status", create_type=False),
            server_default="CREATED",
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_companies_name_nonempty"),
        sa.CheckConstraint("length(btrim(goal)) > 0", name="ck_companies_goal_nonempty"),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="ck_companies_slug_format"),
        sa.PrimaryKeyConstraint("id", name="pk_companies"),
        sa.UniqueConstraint("slug", name="uq_companies_slug"),
    )
    op.create_index("ix_companies_status", "companies", ["status"])

    op.execute(
        """
        CREATE FUNCTION prevent_company_id_update() RETURNS trigger AS $$
        BEGIN
          IF NEW.id <> OLD.id THEN
            RAISE EXCEPTION 'company id is immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_companies_immutable_id BEFORE UPDATE ON companies "
        "FOR EACH ROW EXECUTE FUNCTION prevent_company_id_update()"
    )

    op.create_table(
        "projects",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="project_status", create_type=False),
            server_default="CREATED",
            nullable=False,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_projects_name_nonempty"),
        sa.CheckConstraint("length(btrim(goal)) > 0", name="ck_projects_goal_nonempty"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_projects_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_index("ix_projects_company_id_status", "projects", ["company_id", "status"])
    op.create_index("ix_projects_status", "projects", ["status"])

    op.create_table(
        "agents",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_status", create_type=False),
            server_default="CREATED",
            nullable=False,
        ),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_agents_name_nonempty"),
        sa.CheckConstraint("length(btrim(role)) > 0", name="ck_agents_role_nonempty"),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_agents_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
    )
    op.create_index("ix_agents_company_id_role", "agents", ["company_id", "role"])
    op.create_index("ix_agents_company_id_status", "agents", ["company_id", "status"])
    op.create_index("ix_agents_status", "agents", ["status"])

    op.create_table(
        "tasks",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "acceptance_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="task_status", create_type=False),
            server_default="CREATED",
            nullable=False,
        ),
        sa.Column(
            "priority",
            postgresql.ENUM(name="task_priority", create_type=False),
            server_default="NORMAL",
            nullable=False,
        ),
        sa.Column("iteration", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_iterations", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("iteration >= 0", name="ck_tasks_iteration_nonnegative"),
        sa.CheckConstraint("max_iterations > 0", name="ck_tasks_max_iterations_positive"),
        sa.CheckConstraint("length(btrim(type)) > 0", name="ck_tasks_type_nonempty"),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_tasks_title_nonempty"),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="ck_tasks_completion_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_agent_id"],
            ["agents.id"],
            name="fk_tasks_assigned_agent_id_agents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_tasks_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"],
            ["tasks.id"],
            name="fk_tasks_parent_task_id_tasks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_tasks_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
    op.create_index("ix_tasks_assigned_agent_id", "tasks", ["assigned_agent_id"])
    op.create_index(
        "ix_tasks_company_id_status_created_at", "tasks", ["company_id", "status", "created_at"]
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_index("ix_tasks_priority", "tasks", ["priority"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "task_runs",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="task_run_status", create_type=False),
            server_default="STARTED",
            nullable=False,
        ),
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.CheckConstraint("iteration >= 0", name="ck_task_runs_iteration_nonnegative"),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_task_runs_completion_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_task_runs_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_runs_task_id_tasks", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_runs"),
    )
    op.create_index("ix_task_runs_agent_id", "task_runs", ["agent_id"])
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])

    op.create_table(
        "events",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(type)) > 0", name="ck_events_type_nonempty"),
        sa.CheckConstraint("length(btrim(message)) > 0", name="ck_events_message_nonempty"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_events_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_events_company_id_companies",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_events_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_events_task_id_tasks", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
    )
    op.create_index("ix_events_agent_id", "events", ["agent_id"])
    op.create_index("ix_events_company_id_created_at", "events", ["company_id", "created_at"])
    op.create_index("ix_events_created_at", "events", ["created_at"])
    op.create_index("ix_events_project_id", "events", ["project_id"])
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_index("ix_events_type", "events", ["type"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("task_runs")
    op.drop_table("tasks")
    op.drop_table("agents")
    op.drop_table("projects")
    op.execute("DROP FUNCTION prevent_company_id_update() CASCADE")
    op.drop_table("companies")

    bind = op.get_bind()
    for enum_type in (
        task_run_status,
        task_priority,
        task_status,
        agent_status,
        project_status,
        company_status,
    ):
        enum_type.drop(bind, checkfirst=True)
