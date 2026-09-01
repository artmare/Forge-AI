from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.company import Company
    from app.domain.models.event_consumption import EventConsumption
    from app.domain.models.event_dead_letter import EventDeadLetter
    from app.domain.models.event_outbox import EventOutbox
    from app.domain.models.project import Project
    from app.domain.models.task import Task


class Event(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("event_version > 0", name="event_version_positive"),
        Index("ix_events_company_id_created_at", "company_id", "created_at"),
        Index("ix_events_project_id", "project_id"),
        Index("ix_events_agent_id", "agent_id"),
        Index("ix_events_task_id", "task_id"),
        Index("ix_events_type", "type"),
        Index("ix_events_topic", "topic"),
        Index("ix_events_correlation_id", "correlation_id"),
        Index("ix_events_created_at", "created_at"),
    )

    company_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT")
    )
    agent_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(default=1, server_default="1", nullable=False)
    source: Mapped[str] = mapped_column(
        Text, default="forge-api", server_default="forge-api", nullable=False
    )
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )

    company: Mapped[Company | None] = relationship(back_populates="events")
    project: Mapped[Project | None] = relationship(back_populates="events")
    agent: Mapped[Agent | None] = relationship(back_populates="events")
    task: Mapped[Task | None] = relationship(back_populates="events")
    outbox_entries: Mapped[list[EventOutbox]] = relationship(back_populates="event")
    consumptions: Mapped[list[EventConsumption]] = relationship(back_populates="event")
    dead_letters: Mapped[list[EventDeadLetter]] = relationship(back_populates="event")
