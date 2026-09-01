from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import TaskReviewDecision
from app.domain.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent_run import AgentRun
    from app.domain.models.task import Task
    from app.domain.models.task_run import TaskRun


class TaskReview(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "task_reviews"
    __table_args__ = (
        CheckConstraint("iteration > 0", name="iteration_positive"),
        CheckConstraint(
            "(decision = 'FIX_REQUESTED' AND feedback IS NOT NULL "
            "AND length(btrim(feedback)) > 0) OR "
            "(decision = 'APPROVED' AND feedback IS NULL)",
            name="feedback_matches_decision",
        ),
        CheckConstraint(
            "feedback IS NULL OR length(feedback) <= 10000",
            name="feedback_length_bounded",
        ),
        Index("uq_task_reviews_task_id_iteration", "task_id", "iteration", unique=True),
        Index("ix_task_reviews_task_id_created_at", "task_id", "created_at"),
        Index("ix_task_reviews_task_run_id", "task_run_id"),
        Index("ix_task_reviews_agent_run_id", "agent_run_id"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="RESTRICT")
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT")
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[TaskReviewDecision] = mapped_column(
        Enum(TaskReviewDecision, name="task_review_decision"), nullable=False
    )
    feedback: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )

    task: Mapped[Task] = relationship(back_populates="reviews")
    task_run: Mapped[TaskRun | None] = relationship(back_populates="reviews")
    agent_run: Mapped[AgentRun | None] = relationship(back_populates="reviews")
