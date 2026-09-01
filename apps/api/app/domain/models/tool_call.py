from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import ToolCallStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.agent_run import AgentRun
    from app.domain.models.task import Task
    from app.domain.models.task_run import TaskRun


class ToolCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "(status IN ('REQUESTED', 'AUTHORIZED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'DENIED', 'CANCELLED') "
            "AND completed_at IS NOT NULL)",
            name="completion_matches_status",
        ),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR completed_at >= started_at",
            name="completion_after_start",
        ),
        Index("ix_tool_calls_agent_run_id", "agent_run_id"),
        Index("ix_tool_calls_task_run_id", "task_run_id"),
        Index("ix_tool_calls_task_id_created_at", "task_id", "created_at"),
        Index("ix_tool_calls_agent_id_created_at", "agent_id", "created_at"),
        Index("ix_tool_calls_status_created_at", "status", "created_at"),
        Index(
            "ix_tool_calls_stale_running",
            "started_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    agent_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
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
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(ToolCallStatus, name="tool_call_status"),
        default=ToolCallStatus.REQUESTED,
        server_default=ToolCallStatus.REQUESTED.value,
        nullable=False,
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    permission: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent_run: Mapped[AgentRun] = relationship(back_populates="tool_calls")
    task_run: Mapped[TaskRun] = relationship(back_populates="tool_calls")
    task: Mapped[Task] = relationship(back_populates="tool_calls")
    agent: Mapped[Agent] = relationship(back_populates="tool_calls")

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return max((self.completed_at - self.started_at).total_seconds() * 1000, 0)
