from datetime import datetime
from typing import Any
from uuid import UUID

from app.domain.enums import TaskKind, TaskPriority, TaskStatus, ToolCallStatus
from app.schemas.common import ORMResponse
from app.schemas.development import (
    AcceptanceVerificationResponse,
    DevelopmentExecutionResponse,
    QAResultResponse,
)
from app.schemas.task_review import TaskReviewResponse


class DependencyTaskSummary(ORMResponse):
    id: UUID
    title: str
    status: TaskStatus
    priority: TaskPriority
    assigned_agent_id: UUID | None
    blocked_reason: str | None = None


class TaskDependencyResponse(ORMResponse):
    id: UUID
    task_id: UUID
    depends_on_task_id: UUID
    created_at: datetime
    task: DependencyTaskSummary


class GraphToolCall(ORMResponse):
    id: UUID
    tool_name: str
    status: ToolCallStatus
    result: dict[str, Any] | None
    error: dict[str, Any] | None


class ProjectGraphNode(ORMResponse):
    id: UUID
    title: str
    description: str | None
    kind: TaskKind
    status: TaskStatus
    priority: TaskPriority
    assigned_agent_id: UUID | None
    acceptance_criteria: list[Any]
    iteration: int
    max_iterations: int
    state: str
    ready: bool
    blocked_reason: str | None
    result: dict[str, Any] | None = None
    tool_calls: list[GraphToolCall]
    reviews: list[TaskReviewResponse]
    development_executions: list[DevelopmentExecutionResponse]
    qa_results: list[QAResultResponse]
    acceptance_verifications: list[AcceptanceVerificationResponse]


class ProjectGraphEdge(ORMResponse):
    id: UUID
    task_id: UUID
    depends_on_task_id: UUID


class ProjectGraphResponse(ORMResponse):
    project_id: UUID
    nodes: list[ProjectGraphNode]
    edges: list[ProjectGraphEdge]
