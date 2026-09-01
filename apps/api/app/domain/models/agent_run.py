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

from app.domain.enums import AgentRunStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.task import Task
    from app.domain.models.task_review import TaskReview
    from app.domain.models.task_run import TaskRun
    from app.domain.models.tool_call import ToolCall


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('CREATED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL)",
            name="completion_matches_status",
        ),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cached_tokens >= 0", name="cached_tokens_nonnegative"),
        CheckConstraint("total_tokens >= 0", name="total_tokens_nonnegative"),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0", name="estimated_cost_nonnegative"
        ),
        Index("ix_agent_runs_task_run_id", "task_run_id"),
        Index("ix_agent_runs_task_id_created_at", "task_id", "created_at"),
        Index("ix_agent_runs_agent_id_created_at", "agent_id", "created_at"),
        Index("ix_agent_runs_status_created_at", "status", "created_at"),
        Index(
            "uq_agent_runs_one_running_per_task_run",
            "task_run_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        Index(
            "ix_agent_runs_stale_running",
            "started_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    task_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status"),
        default=AgentRunStatus.CREATED,
        server_default=AgentRunStatus.CREATED.value,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_alias: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provider_response_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    cached_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task_run: Mapped[TaskRun] = relationship(back_populates="agent_runs")
    task: Mapped[Task] = relationship(back_populates="agent_runs")
    agent: Mapped[Agent] = relationship(back_populates="agent_runs")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="agent_run")
    reviews: Mapped[list[TaskReview]] = relationship(back_populates="agent_run")
