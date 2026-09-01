from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import MissionStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.company import Company
    from app.domain.models.planning_run import PlanningRun
    from app.domain.models.project import Project


class Mission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "missions"
    __table_args__ = (
        CheckConstraint("planning_attempts >= 0", name="planning_attempts_nonnegative"),
        CheckConstraint("max_planning_attempts > 0", name="max_planning_attempts_positive"),
        CheckConstraint(
            "planning_attempts <= max_planning_attempts",
            name="planning_attempts_within_maximum",
        ),
        CheckConstraint("paid_ai_budget >= 0", name="mission_ai_budget_nonnegative"),
        CheckConstraint("ai_spend_recorded >= 0", name="mission_ai_spend_nonnegative"),
        Index("ix_missions_company_id_status", "company_id", "status"),
        Index("ix_missions_project_id", "project_id", unique=True),
        Index("ix_missions_status_created_at", "status", "created_at"),
        Index("ix_missions_archived_at", "archived_at"),
    )

    company_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[MissionStatus] = mapped_column(
        Enum(MissionStatus, name="mission_status"),
        default=MissionStatus.DRAFT,
        server_default=MissionStatus.DRAFT.value,
        nullable=False,
    )
    planning_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    max_planning_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    planning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planning_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    paid_ai_budget: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    ai_spend_recorded: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owns_company: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    owns_project: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    company: Mapped[Company | None] = relationship(back_populates="missions")
    project: Mapped[Project | None] = relationship(back_populates="mission")
    planning_runs: Mapped[list[PlanningRun]] = relationship(back_populates="mission")


class MissionDeletionRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable tombstone for DB deletion and non-transactional workspace cleanup."""

    __tablename__ = "mission_deletion_records"
    __table_args__ = (
        Index("ix_mission_deletion_records_mission_id", "mission_id", unique=True),
        Index("ix_mission_deletion_records_cleanup_status", "workspace_cleanup_status"),
    )

    mission_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    mission_title: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    project_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    company_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    project_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    workspace_cleanup_status: Mapped[str] = mapped_column(
        Text, default="REQUESTED", server_default="REQUESTED", nullable=False
    )
    workspace_cleanup_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
