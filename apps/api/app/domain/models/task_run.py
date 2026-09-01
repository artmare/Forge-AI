from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import TaskRunStatus
from app.domain.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.agent_run import AgentRun
    from app.domain.models.execution_job import ExecutionJob
    from app.domain.models.task import Task
    from app.domain.models.task_review import TaskReview
    from app.domain.models.tool_call import ToolCall


class TaskRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        CheckConstraint("iteration > 0", name="iteration_positive"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at", name="completion_after_start"
        ),
        CheckConstraint(
            "(status = 'STARTED' AND completed_at IS NULL) OR "
            "(status <> 'STARTED' AND completed_at IS NOT NULL)",
            name="completion_matches_status",
        ),
        Index("ix_task_runs_task_id", "task_id"),
        Index("ix_task_runs_agent_id", "agent_id"),
        Index("uq_task_runs_task_id_iteration", "task_id", "iteration", unique=True),
        Index(
            "uq_task_runs_one_started_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'STARTED'"),
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TaskRunStatus] = mapped_column(
        Enum(TaskRunStatus, name="task_run_status"),
        default=TaskRunStatus.STARTED,
        server_default=TaskRunStatus.STARTED.value,
        nullable=False,
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[Any | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )

    task: Mapped[Task] = relationship(back_populates="runs")
    agent: Mapped[Agent] = relationship(back_populates="task_runs")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="task_run")
    reviews: Mapped[list[TaskReview]] = relationship(back_populates="task_run")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="task_run")
    execution_jobs: Mapped[list[ExecutionJob]] = relationship(back_populates="task_run")
