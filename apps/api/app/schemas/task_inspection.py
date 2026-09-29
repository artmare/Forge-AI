from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.domain.enums import (
    AgentRunStatus,
    DevelopmentExecutionStatus,
    TaskKind,
    TaskPriority,
    TaskReviewDecision,
    TaskRunStatus,
    TaskStatus,
    ToolCallStatus,
)


class InspectionAgent(BaseModel):
    id: UUID
    name: str
    role: str


class InspectionError(BaseModel):
    code: str
    message: str


class InspectionTaskLink(BaseModel):
    id: UUID
    title: str
    status: TaskStatus
    blocked_reason: str | None = None


class InspectionTaskRun(BaseModel):
    id: UUID
    iteration: int
    status: TaskRunStatus
    error: Any | None
    started_at: datetime
    completed_at: datetime | None


class InspectionAgentRun(BaseModel):
    id: UUID
    agent: InspectionAgent
    status: AgentRunStatus
    provider: str
    model_alias: str
    model_id: str
    error: InspectionError | None
    total_tokens: int
    started_at: datetime | None
    completed_at: datetime | None
    recovery_attempts: list[Any] = []


class InspectionCommand(BaseModel):
    id: UUID
    action: str
    status: DevelopmentExecutionStatus
    safe_arguments: dict[str, Any]
    execution_origin: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    exit_code: int | None
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    output_truncated: bool
    error: InspectionError | None
    change_summary: dict[str, Any] | None


class InspectionToolCall(BaseModel):
    id: UUID
    tool_name: str
    status: ToolCallStatus
    safe_arguments: dict[str, Any]
    safe_result: dict[str, Any] | None
    duration_ms: float | None
    started_at: datetime | None
    completed_at: datetime | None
    error: InspectionError | None


class InspectionQAResult(BaseModel):
    id: UUID
    decision: str
    summary: str
    failure_classification: str | None
    failure_code: str | None
    checks: list[Any]
    blocking_issues: list[Any]
    non_blocking_issues: list[Any]
    deterministic_checks_executed: bool
    created_at: datetime


class InspectionDevelopmentProfile(BaseModel):
    project_type: str
    package_manager: str
    detection_source: str
    available_actions: list[str]
    unavailable_actions: list[str]
    repository_initialized: bool
    repository_branch: str | None
    initial_checkpoint_created: bool
    changed_files_count: int
    last_refreshed_at: datetime
    bootstrap_attempts: int
    bootstrap_error: InspectionError | None


class InspectionAcceptance(BaseModel):
    id: UUID | None = None
    criterion_index: int
    criterion: str
    status: str
    verifier: str | None = None
    evidence_summary: str
    execution_ids: list[Any]
    created_at: datetime | None = None


class InspectionReview(BaseModel):
    id: UUID
    iteration: int
    decision: TaskReviewDecision
    feedback: str | None
    created_at: datetime


class InspectionIteration(BaseModel):
    iteration: int
    task_run: InspectionTaskRun
    agent_runs: list[InspectionAgentRun]
    commands: list[InspectionCommand]
    tool_calls: list[InspectionToolCall]
    qa_result: InspectionQAResult | None
    acceptance_criteria: list[InspectionAcceptance]
    reviews: list[InspectionReview]


class InspectionExecutionJob(BaseModel):
    id: UUID
    status: str
    phase: str
    attempts: int
    max_attempts: int
    worker_id: UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: Any | None
    failure_evidence: Any | None
    phase_history: list[Any]
    retry_history: list[Any]
    environment_state: dict[str, Any]
    checkpoint_state: dict[str, Any]
    working_tree_state: dict[str, Any]


class TaskFailureSummary(BaseModel):
    category: str
    error_code: str
    message: str
    failing_phase: str | None
    failing_command: str | None
    command_exit_code: int | None
    qa_failure_reason: str | None
    failed_acceptance_criteria: list[InspectionAcceptance]
    iteration_exhausted: bool
    provider: str | None
    model: str | None
    tool_failure: InspectionError | None
    failed_at: datetime | None = None
    attempt: int | None = None
    agent_role: str | None = None
    recovery_attempts: list[Any] = []
    evidence_source: str
    budget_diagnostics: dict[str, Any] | None = None


class TaskInspectionResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    kind: TaskKind
    status: TaskStatus
    priority: TaskPriority
    iteration: int
    max_iterations: int
    acceptance_criteria: list[Any]
    assigned_agent: InspectionAgent | None
    development_profile: InspectionDevelopmentProfile | None
    dependencies: list[InspectionTaskLink]
    dependents: list[InspectionTaskLink]
    iterations: list[InspectionIteration]
    execution_jobs: list[InspectionExecutionJob]
    final_acceptance_criteria: list[InspectionAcceptance]
    failure: TaskFailureSummary | None
