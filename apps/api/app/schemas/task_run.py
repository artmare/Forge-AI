from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.domain.enums import TaskRunStatus
from app.schemas.common import ORMResponse


class TaskRunCreate(ORMResponse):
    task_id: UUID
    agent_id: UUID
    iteration: int = Field(ge=0)
    input: dict[str, Any] = Field(default_factory=dict)


class TaskRunUpdate(ORMResponse):
    status: TaskRunStatus | None = None
    output: dict[str, Any] | None = None
    error: Any | None = None
    completed_at: datetime | None = None


class TaskRunResponse(ORMResponse):
    id: UUID
    task_id: UUID
    agent_id: UUID
    iteration: int
    status: TaskRunStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: Any | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
