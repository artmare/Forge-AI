from datetime import datetime
from typing import Any
from uuid import UUID

from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    DevelopmentProjectType,
    PackageManager,
    QADecision,
)
from app.schemas.common import ORMResponse


class DevelopmentProfileResponse(ORMResponse):
    id: UUID
    project_id: UUID
    project_type: DevelopmentProjectType
    package_manager: PackageManager
    install_action: DevelopmentAction | None
    test_action: DevelopmentAction | None
    build_action: DevelopmentAction | None
    lint_action: DevelopmentAction | None
    typecheck_action: DevelopmentAction | None
    detection_source: str
    repository_initialized: bool
    repository_branch: str | None
    initial_checkpoint_created: bool
    changed_files_count: int
    last_refreshed_at: datetime
    bootstrap_attempts: int
    bootstrap_error_code: str | None
    bootstrap_error_message: str | None
    created_at: datetime
    updated_at: datetime


class DevelopmentExecutionResponse(ORMResponse):
    id: UUID
    company_id: UUID
    project_id: UUID
    task_id: UUID
    task_run_id: UUID | None
    agent_run_id: UUID | None
    agent_id: UUID
    action: DevelopmentAction
    status: DevelopmentExecutionStatus
    working_directory: str
    safe_arguments: dict[str, Any]
    execution_origin: str
    exit_code: int | None
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    network_enabled: bool
    change_summary: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    timeout_seconds: int
    error_code: str | None
    error_message: str | None
    created_at: datetime


class QAResultResponse(ORMResponse):
    id: UUID
    task_id: UUID
    task_run_id: UUID
    verifier_agent_id: UUID | None
    iteration: int
    decision: QADecision
    summary: str
    failure_classification: str | None
    failure_code: str | None
    checks: list[Any]
    blocking_issues: list[Any]
    non_blocking_issues: list[Any]
    created_at: datetime


class AcceptanceVerificationResponse(ORMResponse):
    id: UUID
    task_id: UUID
    task_run_id: UUID
    criterion_index: int
    criterion: str
    iteration: int
    status: AcceptanceVerificationStatus
    verifier: str
    verifier_agent_id: UUID | None
    evidence_summary: str
    execution_ids: list[Any]
    created_at: datetime


class DevelopmentSummaryResponse(ORMResponse):
    task_id: UUID
    current_stage: str
    latest_change_summary: dict[str, Any] | None
    executions: list[DevelopmentExecutionResponse]
    qa_results: list[QAResultResponse]
    acceptance_verifications: list[AcceptanceVerificationResponse]
