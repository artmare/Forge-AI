"""Phase 04 transactional event system.

Revision ID: 20260820_0003
Revises: 20260819_0002
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260820_0003"
down_revision: str | None = "20260819_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

outbox_status = postgresql.ENUM(
    "PENDING", "PROCESSING", "PUBLISHED", "FAILED", name="outbox_status"
)
consumption_status = postgresql.ENUM(
    "PROCESSING",
    "RETRYING",
    "SUCCEEDED",
    "FAILED",
    name="event_consumption_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    outbox_status.create(bind, checkfirst=True)
    consumption_status.create(bind, checkfirst=True)

    op.alter_column("events", "company_id", existing_type=postgresql.UUID(), nullable=True)
    op.add_column("events", sa.Column("topic", sa.Text(), nullable=True))
    op.add_column(
        "events",
        sa.Column("event_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "events",
        sa.Column("source", sa.Text(), server_default="forge-api", nullable=False),
    )
    op.add_column("events", sa.Column("correlation_id", postgresql.UUID(), nullable=True))
    op.add_column("events", sa.Column("causation_id", postgresql.UUID(), nullable=True))
    op.execute(
        """
        UPDATE events
        SET topic = CASE
          WHEN type LIKE 'COMPANY_%' THEN 'forge.company'
          WHEN type LIKE 'PROJECT_%' THEN 'forge.project'
          WHEN type LIKE 'AGENT_%' THEN 'forge.agent'
          WHEN type LIKE 'TASK_RUN_%' THEN 'forge.task_run'
          WHEN type LIKE 'TASK_%' THEN 'forge.task'
          ELSE 'forge.system'
        END,
        correlation_id = COALESCE(task_id, project_id, agent_id, company_id, id)
        """
    )
    op.alter_column("events", "topic", existing_type=sa.Text(), nullable=False)
    op.alter_column("events", "correlation_id", existing_type=postgresql.UUID(), nullable=False)
    op.create_check_constraint("ck_events_event_version_positive", "events", "event_version > 0")
    op.create_index("ix_events_topic", "events", ["topic"])
    op.create_index("ix_events_correlation_id", "events", ["correlation_id"])

    op.create_table(
        "event_outbox",
        sa.Column("event_id", postgresql.UUID(), nullable=False),
        sa.Column("replay_of_outbox_id", postgresql.UUID(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="outbox_status", create_type=False),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column(
            "id",
            postgresql.UUID(),
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
        sa.CheckConstraint("attempts >= 0", name="ck_event_outbox_attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name="fk_event_outbox_event_id_events", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["replay_of_outbox_id"],
            ["event_outbox.id"],
            name="fk_event_outbox_replay_of_outbox_id_event_outbox",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_outbox"),
    )
    op.create_index("ix_event_outbox_event_id", "event_outbox", ["event_id"])
    op.create_index("ix_event_outbox_topic", "event_outbox", ["topic"])
    op.create_index(
        "ix_event_outbox_status_available_at",
        "event_outbox",
        ["status", "available_at"],
    )
    op.create_index(
        "ix_event_outbox_publishable",
        "event_outbox",
        ["available_at"],
        postgresql_where=sa.text("status IN ('PENDING', 'FAILED')"),
    )

    op.create_table(
        "event_consumptions",
        sa.Column("event_id", postgresql.UUID(), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="event_consumption_status", create_type=False),
            server_default="PROCESSING",
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id",
            postgresql.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_event_consumptions_attempt_nonnegative"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_consumptions_event_id_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_consumptions"),
        sa.UniqueConstraint("event_id", "consumer", name="uq_event_consumptions_event_consumer"),
    )
    op.create_index(
        "ix_event_consumptions_consumer_status",
        "event_consumptions",
        ["consumer", "status"],
    )

    op.create_table(
        "event_dead_letters",
        sa.Column("event_id", postgresql.UUID(), nullable=False),
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("original_event", postgresql.JSONB(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("redis_message_id", sa.Text(), nullable=True),
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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
            postgresql.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts > 0", name="ck_event_dead_letters_attempts_positive"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name="fk_event_dead_letters_event_id_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_dead_letters"),
        sa.UniqueConstraint("event_id", "consumer", name="uq_event_dead_letters_event_consumer"),
    )
    op.create_index(
        "ix_event_dead_letters_consumer_failed_at",
        "event_dead_letters",
        ["consumer", "failed_at"],
    )
    op.create_index("ix_event_dead_letters_event_type", "event_dead_letters", ["event_type"])

    op.create_table(
        "event_worker_health",
        sa.Column("worker", sa.Text(), nullable=False),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "id",
            postgresql.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_worker_health"),
        sa.UniqueConstraint("worker", name="uq_event_worker_health_worker"),
    )
    op.create_index("ix_event_worker_health_heartbeat_at", "event_worker_health", ["heartbeat_at"])

    # Existing persistent events are publishable after upgrade. Their original
    # identity and occurrence time are preserved in the backfilled envelope.
    op.execute(
        """
        INSERT INTO event_outbox (event_id, topic, payload)
        SELECT
          id,
          topic,
          jsonb_build_object(
            'event_id', id::text,
            'event_type', type,
            'event_version', event_version,
            'occurred_at', to_char(
              created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            ),
            'source', source,
            'company_id', company_id::text,
            'project_id', project_id::text,
            'agent_id', agent_id::text,
            'task_id', task_id::text,
            'correlation_id', correlation_id::text,
            'causation_id', causation_id::text,
            'payload', jsonb_build_object('message', message) || metadata
          )
        FROM events
        ORDER BY created_at, id
        """
    )


def downgrade() -> None:
    op.drop_table("event_worker_health")
    op.drop_table("event_dead_letters")
    op.drop_table("event_consumptions")
    op.drop_table("event_outbox")

    op.drop_index("ix_events_correlation_id", table_name="events")
    op.drop_index("ix_events_topic", table_name="events")
    op.drop_constraint("ck_events_event_version_positive", "events", type_="check")
    op.drop_column("events", "causation_id")
    op.drop_column("events", "correlation_id")
    op.drop_column("events", "source")
    op.drop_column("events", "event_version")
    op.drop_column("events", "topic")
    op.execute("DELETE FROM events WHERE company_id IS NULL")
    op.alter_column("events", "company_id", existing_type=postgresql.UUID(), nullable=False)

    bind = op.get_bind()
    consumption_status.drop(bind, checkfirst=True)
    outbox_status.drop(bind, checkfirst=True)
