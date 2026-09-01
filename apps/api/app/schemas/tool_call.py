from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.domain.enums import ToolCallStatus
from app.schemas.common import ORMResponse


class ToolCallResponse(ORMResponse):
    id: UUID
    agent_run_id: UUID
    task_run_id: UUID
    task_id: UUID
    agent_id: UUID
    tool_name: str
    status: ToolCallStatus
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    permission: str | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: float | None
    created_at: datetime
    updated_at: datetime


class RecentToolCallResponse(ToolCallResponse):
    task_title: str
    agent_name: str


class ToolCallStatsResponse(BaseModel):
    total: int
    by_status: dict[ToolCallStatus, int]
    average_duration_ms: float | None
