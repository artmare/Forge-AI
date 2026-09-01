from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ExecutionJobStatus, ExecutionPhase, TaskPriority, WorkerStatus


class ExecutionJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    task_run_id: UUID | None
    agent_id: UUID
    worker_id: UUID | None
    status: ExecutionJobStatus
    phase: ExecutionPhase
    phase_history: list[Any]
    failure_evidence: dict[str, Any] | None
    retry_history: list[Any]
    environment_state: dict[str, Any]
    checkpoint_state: dict[str, Any]
    working_tree_state: dict[str, Any]
    priority: TaskPriority
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    last_error: dict[str, Any] | None
    correlation_id: UUID
    created_at: datetime
    updated_at: datetime


class WorkerResponse(BaseModel):
    id: UUID
    worker_key: str
    status: WorkerStatus
    concurrency: int
    started_at: datetime
    last_heartbeat_at: datetime
    stopped_at: datetime | None
    active_jobs: int
    created_at: datetime
    updated_at: datetime


class OrchestratorStatusResponse(BaseModel):
    autonomy_enabled: bool
    orchestrator_online: bool
    queued_jobs: int
    running_jobs: int
    failed_jobs: int
    active_workers: int
    stale_workers: int
    queued_tasks_without_jobs: int
    last_reconciliation_time: datetime | None
    total_jobs: int
    job_counts: dict[ExecutionJobStatus, int]
    average_execution_duration_ms: float | None


class OrchestratorHealthResponse(BaseModel):
    status: str
    autonomy_enabled: bool
    orchestrator_online: bool
    last_reconciliation_time: datetime | None
    active_workers: int
    stale_workers: int
    queued_jobs: int
    running_jobs: int
