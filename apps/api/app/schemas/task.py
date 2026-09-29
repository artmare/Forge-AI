from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ConfigDict, Field

from app.domain.enums import TaskKind, TaskPriority, TaskStatus
from app.schemas.common import NonEmptyText, ORMResponse, PartialUpdate


class TaskCreate(ORMResponse):
    company_id: UUID
    project_id: UUID | None = None
    assigned_agent_id: UUID | None = None
    parent_task_id: UUID | None = None
    type: NonEmptyText
    kind: TaskKind = TaskKind.GENERAL
    title: NonEmptyText
    description: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    priority: TaskPriority = TaskPriority.NORMAL
    max_iterations: int = Field(default=1, gt=0)
    paid_ai_budget: Decimal | None = Field(default=None, ge=0)


class TaskUpdate(PartialUpdate):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    non_nullable_fields = frozenset(
        {
            "type",
            "kind",
            "title",
            "input",
            "acceptance_criteria",
            "priority",
            "max_iterations",
        }
    )

    project_id: UUID | None = None
    assigned_agent_id: UUID | None = None
    parent_task_id: UUID | None = None
    type: NonEmptyText | None = None
    kind: TaskKind | None = None
    title: NonEmptyText | None = None
    description: str | None = None
    input: dict[str, Any] | None = None
    acceptance_criteria: list[Any] | None = None
    priority: TaskPriority | None = None
    max_iterations: int | None = Field(default=None, gt=0)
    paid_ai_budget: Decimal | None = Field(default=None, ge=0)


class TaskTransitionRequest(ORMResponse):
    model_config = ConfigDict(extra="forbid")

    target_status: TaskStatus
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class TaskBudgetResumeRequest(ORMResponse):
    model_config = ConfigDict(extra="forbid")

    additional_input_tokens: int = Field(ge=1024, le=1_000_000)


class TaskResponse(ORMResponse):
    id: UUID
    company_id: UUID
    project_id: UUID | None
    assigned_agent_id: UUID | None
    parent_task_id: UUID | None
    type: str
    kind: TaskKind
    title: str
    description: str | None
    input: dict[str, Any]
    acceptance_criteria: list[Any]
    terminal_reason: dict[str, Any] | None
    status: TaskStatus
    priority: TaskPriority
    iteration: int
    max_iterations: int
    paid_ai_budget: Decimal | None
    ai_spend_recorded: Decimal
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
