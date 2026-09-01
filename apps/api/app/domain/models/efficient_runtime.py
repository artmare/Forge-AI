from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskRuntimeBudget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_runtime_budgets"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_task_runtime_budgets_task_id"),
        CheckConstraint("max_model_calls > 0", name="max_model_calls_positive"),
        CheckConstraint("max_calls_per_iteration > 0", name="max_calls_per_iteration_positive"),
        CheckConstraint("max_input_tokens > 0", name="max_input_tokens_positive"),
        CheckConstraint("max_output_tokens > 0", name="max_output_tokens_positive"),
        CheckConstraint("max_free_model_calls > 0", name="max_free_model_calls_positive"),
        CheckConstraint("max_paid_model_calls > 0", name="max_paid_model_calls_positive"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_calls_per_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_free_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_paid_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    consumed_model_calls: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    consumed_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    consumed_output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    consumed_cached_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    consumed_estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    current_model_alias: Mapped[str | None] = mapped_column(Text)
    warning_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    stopped_reason: Mapped[str | None] = mapped_column(Text)


class ModelEscalation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "model_escalations"
    __table_args__ = (Index("ix_model_escalations_task_id_created_at", "task_id", "created_at"),)

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="SET NULL")
    )
    from_alias: Mapped[str] = mapped_column(Text, nullable=False)
    to_alias: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    objective_signals: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class TaskRuntimeMetric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "task_runtime_metrics"
    __table_args__ = (UniqueConstraint("task_id", name="uq_task_runtime_metrics_task_id"),)

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    context_bytes_sent: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    estimated_unchanged_bytes_avoided: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    repeated_reads_avoided: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    duplicate_turns_detected: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    deterministic_executions: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    tool_signature_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )


class FileContextCache(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "file_context_cache"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_file_context_cache_project_path"),
        Index("ix_file_context_cache_project_id_updated_at", "project_id", "updated_at"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_model_visible_hash: Mapped[str | None] = mapped_column(Text)
    safe_summary: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)


class ProjectKnowledgeIndex(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_knowledge_indexes"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_knowledge_indexes_project_id"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    architecture_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    modules: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    contracts: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    decisions: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    constraints: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    recent_changes: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )


class ProductQAResult(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "product_qa_results"
    __table_args__ = (
        Index("ix_product_qa_results_task_id_created_at", "task_id", "created_at"),
        UniqueConstraint("task_id", "iteration", name="uq_product_qa_results_task_iteration"),
    )

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    task_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    issues: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    viewport_contract: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    screenshot_references: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
