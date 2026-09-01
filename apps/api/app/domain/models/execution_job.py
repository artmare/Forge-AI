from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import ExecutionJobStatus, ExecutionPhase, TaskPriority
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.task import Task
    from app.domain.models.task_run import TaskRun
    from app.domain.models.worker_node import WorkerNode


class ExecutionJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "execution_jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        CheckConstraint("attempts <= max_attempts", name="attempts_within_maximum"),
        CheckConstraint(
            "started_at IS NULL OR completed_at IS NULL OR completed_at >= started_at",
            name="completion_after_start",
        ),
        CheckConstraint(
            "(status IN ('PENDING', 'CLAIMED', 'RUNNING') AND completed_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL)",
            name="completion_matches_status",
        ),
        CheckConstraint(
            "status = 'PENDING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="active_lease_present",
        ),
        Index("ix_execution_jobs_task_id", "task_id"),
        Index("ix_execution_jobs_task_run_id", "task_run_id"),
        Index("ix_execution_jobs_agent_id", "agent_id"),
        Index("ix_execution_jobs_worker_id", "worker_id"),
        Index("ix_execution_jobs_status_available_at", "status", "available_at"),
        Index("ix_execution_jobs_lease_expires_at", "lease_expires_at"),
        Index(
            "uq_execution_jobs_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'CLAIMED', 'RUNNING')"),
        ),
        Index(
            "uq_execution_jobs_one_running_per_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("status IN ('CLAIMED', 'RUNNING')"),
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    task_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="RESTRICT")
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    worker_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("worker_nodes.id", ondelete="SET NULL")
    )
    status: Mapped[ExecutionJobStatus] = mapped_column(
        Enum(ExecutionJobStatus, name="execution_job_status"),
        default=ExecutionJobStatus.PENDING,
        server_default=ExecutionJobStatus.PENDING.value,
        nullable=False,
    )
    phase: Mapped[ExecutionPhase] = mapped_column(
        Enum(ExecutionPhase, name="execution_phase"),
        default=ExecutionPhase.PREPARING,
        server_default=ExecutionPhase.PREPARING.value,
        nullable=False,
    )
    phase_history: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    failure_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    internal_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retry_history: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    environment_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    checkpoint_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    working_tree_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(TaskPriority, name="task_priority", create_type=False), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)

    task: Mapped[Task] = relationship(back_populates="execution_jobs")
    task_run: Mapped[TaskRun | None] = relationship(back_populates="execution_jobs")
    agent: Mapped[Agent] = relationship(back_populates="execution_jobs")
    worker: Mapped[WorkerNode | None] = relationship(back_populates="execution_jobs")
