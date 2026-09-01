from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
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

from app.domain.enums import PlanningRunStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.mission import Mission


class PlanningRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "planning_runs"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cached_tokens >= 0", name="cached_tokens_nonnegative"),
        CheckConstraint("provider_attempts >= 0", name="provider_attempts_nonnegative"),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name="estimated_cost_nonnegative",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="completion_after_start",
        ),
        Index("ix_planning_runs_mission_id_created_at", "mission_id", "created_at"),
        Index("ix_planning_runs_status_started_at", "status", "started_at"),
        Index(
            "uq_planning_runs_one_active_per_mission",
            "mission_id",
            unique=True,
            postgresql_where=text("status IN ('CREATED', 'RUNNING')"),
        ),
    )

    mission_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("missions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[PlanningRunStatus] = mapped_column(
        Enum(PlanningRunStatus, name="planning_run_status"),
        default=PlanningRunStatus.CREATED,
        server_default=PlanningRunStatus.CREATED.value,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_alias: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_model: Mapped[str] = mapped_column(Text, nullable=False)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provider_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    retry_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    internal_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    cached_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    economic_tier: Mapped[str | None] = mapped_column(Text)
    selection_reason: Mapped[str | None] = mapped_column(Text)
    required_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    fallback_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mission: Mapped[Mission] = relationship(back_populates="planning_runs")
