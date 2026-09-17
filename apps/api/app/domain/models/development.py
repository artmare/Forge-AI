from __future__ import annotations

from datetime import datetime
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

from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    DevelopmentProjectType,
    PackageManager,
    QADecision,
)
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.project import Project


class ProjectDevelopmentProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_development_profiles"

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    project_type: Mapped[DevelopmentProjectType] = mapped_column(
        Enum(DevelopmentProjectType, name="development_project_type"), nullable=False
    )
    package_manager: Mapped[PackageManager] = mapped_column(
        Enum(PackageManager, name="development_package_manager"), nullable=False
    )
    install_action: Mapped[DevelopmentAction | None] = mapped_column(
        Enum(DevelopmentAction, name="development_action")
    )
    test_action: Mapped[DevelopmentAction | None] = mapped_column(
        Enum(DevelopmentAction, name="development_action", create_type=False)
    )
    build_action: Mapped[DevelopmentAction | None] = mapped_column(
        Enum(DevelopmentAction, name="development_action", create_type=False)
    )
    lint_action: Mapped[DevelopmentAction | None] = mapped_column(
        Enum(DevelopmentAction, name="development_action", create_type=False)
    )
    typecheck_action: Mapped[DevelopmentAction | None] = mapped_column(
        Enum(DevelopmentAction, name="development_action", create_type=False)
    )
    detection_source: Mapped[str] = mapped_column(Text, nullable=False)
    repository_initialized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    repository_branch: Mapped[str | None] = mapped_column(Text)
    initial_checkpoint_created: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    changed_files_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    bootstrap_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    bootstrap_error_code: Mapped[str | None] = mapped_column(Text)
    bootstrap_error_message: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="development_profile")


class ProjectDevelopmentLease(Base):
    __tablename__ = "project_development_leases"

    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="development_lease")


class DevelopmentExecution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "development_executions"
    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
        CheckConstraint("stdout_bytes >= 0 AND stderr_bytes >= 0", name="output_bytes_nonnegative"),
        CheckConstraint(
            "execution_origin IN ('MODEL_REQUESTED', 'FORGE_QA')",
            name="execution_origin_valid",
        ),
        Index("ix_development_executions_task_id_created_at", "task_id", "created_at"),
        Index("ix_development_executions_project_id_status", "project_id", "status"),
        Index("ix_development_executions_agent_run_id", "agent_run_id"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    task_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("task_runs.id", ondelete="RESTRICT")
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="RESTRICT")
    )
    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[DevelopmentAction] = mapped_column(
        Enum(DevelopmentAction, name="development_action", create_type=False), nullable=False
    )
    status: Mapped[DevelopmentExecutionStatus] = mapped_column(
        Enum(DevelopmentExecutionStatus, name="development_execution_status"), nullable=False
    )
    working_directory: Mapped[str] = mapped_column(Text, nullable=False)
    safe_arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    execution_origin: Mapped[str] = mapped_column(
        Text, default="MODEL_REQUESTED", server_default="MODEL_REQUESTED", nullable=False
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_excerpt: Mapped[str | None] = mapped_column(Text)
    stderr_excerpt: Mapped[str | None] = mapped_column(Text)
    stdout_bytes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    stderr_bytes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    network_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    change_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)


class QAResult(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "qa_results"
    __table_args__ = (
        Index("uq_qa_results_task_id_iteration", "task_id", "iteration", unique=True),
        Index("ix_qa_results_task_id_created_at", "task_id", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    task_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verifier_agent_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT")
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[QADecision] = mapped_column(
        Enum(QADecision, name="qa_decision"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    failure_classification: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    blocking_issues: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    non_blocking_issues: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    correlation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class AcceptanceVerification(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "acceptance_verifications"
    __table_args__ = (
        Index(
            "uq_acceptance_verifications_task_iteration_criterion",
            "task_id",
            "iteration",
            "criterion_index",
            unique=True,
        ),
        Index("ix_acceptance_verifications_task_id_created_at", "task_id", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    task_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("task_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    criterion_index: Mapped[int] = mapped_column(Integer, nullable=False)
    criterion: Mapped[str] = mapped_column(Text, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AcceptanceVerificationStatus] = mapped_column(
        Enum(AcceptanceVerificationStatus, name="acceptance_verification_status"), nullable=False
    )
    verifier: Mapped[str] = mapped_column(Text, nullable=False)
    verifier_agent_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT")
    )
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False)
    execution_ids: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
