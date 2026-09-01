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

from app.domain.enums import TaskKind, TaskPriority, TaskStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.agent_run import AgentRun
    from app.domain.models.company import Company
    from app.domain.models.event import Event
    from app.domain.models.execution_job import ExecutionJob
    from app.domain.models.project import Project
    from app.domain.models.task_dependency import TaskDependency
    from app.domain.models.task_review import TaskReview
    from app.domain.models.task_run import TaskRun
    from app.domain.models.tool_call import ToolCall


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("iteration >= 0", name="iteration_nonnegative"),
        CheckConstraint("iteration <= max_iterations", name="iteration_within_maximum"),
        CheckConstraint("max_iterations > 0", name="max_iterations_positive"),
        CheckConstraint(
            "paid_ai_budget IS NULL OR paid_ai_budget >= 0",
            name="task_ai_budget_nonnegative",
        ),
        CheckConstraint("ai_spend_recorded >= 0", name="task_ai_spend_nonnegative"),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completion_after_start",
        ),
        CheckConstraint(
            "(status IN ('DONE', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('DONE', 'FAILED', 'CANCELLED') AND completed_at IS NULL)",
            name="terminal_completion",
        ),
        CheckConstraint(
            "status <> 'IN_PROGRESS' OR started_at IS NOT NULL",
            name="in_progress_started",
        ),
        Index("ix_tasks_company_id_status_created_at", "company_id", "status", "created_at"),
        Index("ix_tasks_project_id", "project_id"),
        Index("ix_tasks_assigned_agent_id", "assigned_agent_id"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_priority", "priority"),
        Index("ix_tasks_created_at", "created_at"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT")
    )
    assigned_agent_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT")
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[TaskKind] = mapped_column(
        Enum(TaskKind, name="task_kind"),
        default=TaskKind.GENERAL,
        server_default=TaskKind.GENERAL.value,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    acceptance_criteria: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"),
        default=TaskStatus.CREATED,
        server_default=TaskStatus.CREATED.value,
        nullable=False,
    )
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name="task_priority"),
        default=TaskPriority.NORMAL,
        server_default=TaskPriority.NORMAL.value,
        nullable=False,
    )
    iteration: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    max_iterations: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    paid_ai_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ai_spend_recorded: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped[Company] = relationship(back_populates="tasks")
    project: Mapped[Project | None] = relationship(back_populates="tasks")
    assigned_agent: Mapped[Agent | None] = relationship(back_populates="assigned_tasks")
    parent_task: Mapped[Task | None] = relationship(
        remote_side="Task.id", back_populates="subtasks"
    )
    subtasks: Mapped[list[Task]] = relationship(back_populates="parent_task")
    runs: Mapped[list[TaskRun]] = relationship(back_populates="task")
    reviews: Mapped[list[TaskReview]] = relationship(back_populates="task")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="task")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="task")
    events: Mapped[list[Event]] = relationship(back_populates="task")
    execution_jobs: Mapped[list[ExecutionJob]] = relationship(back_populates="task")
    dependencies: Mapped[list[TaskDependency]] = relationship(
        foreign_keys="TaskDependency.task_id", back_populates="task"
    )
    dependents: Mapped[list[TaskDependency]] = relationship(
        foreign_keys="TaskDependency.depends_on_task_id", back_populates="depends_on_task"
    )
