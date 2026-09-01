from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ModelCallRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_call_records"
    __table_args__ = (
        CheckConstraint("reserved_cost >= 0", name="model_call_reserved_cost_nonnegative"),
        CheckConstraint("estimated_cost >= 0", name="model_call_estimated_cost_nonnegative"),
        Index("ix_model_calls_company_created", "company_id", "created_at"),
        Index("ix_model_calls_mission_created", "mission_id", "created_at"),
        Index("ix_model_calls_task_created", "task_id", "created_at"),
    )

    company_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL")
    )
    mission_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("missions.id", ondelete="SET NULL")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL")
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    planning_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("planning_runs.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    economic_tier: Mapped[str] = mapped_column(Text, nullable=False)
    paid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    agent_role: Mapped[str] = mapped_column(Text, nullable=False)
    selection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    required_capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]"), nullable=False
    )
    fallback_from_provider: Mapped[str | None] = mapped_column(Text)
    fallback_from_model: Mapped[str | None] = mapped_column(Text)
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    reserved_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    cached_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelProviderHealth(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_provider_health"
    __table_args__ = (
        UniqueConstraint("provider", "model_id", name="uq_model_provider_health_target"),
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="HEALTHY")
    failure_code: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
