from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import AgentRunStatus
from app.schemas.common import ORMResponse


class ExecuteTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_alias: str = Field(default="default", min_length=1, max_length=64)


class AgentRunResponse(ORMResponse):
    id: UUID
    task_run_id: UUID
    task_id: UUID
    agent_id: UUID
    status: AgentRunStatus
    provider: str
    model_alias: str
    model_id: str
    request: dict[str, Any]
    response: dict[str, Any] | None
    provider_response_id: str | None
    error_code: str | None
    error_message: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    estimated_cost: Decimal | None
    economic_tier: str | None
    selection_reason: str | None
    required_capabilities: list[str]
    fallback_history: list[dict[str, Any]]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RecentAgentRunResponse(AgentRunResponse):
    task_title: str
    agent_name: str
    agent_role: str


class AgentRunStatsResponse(BaseModel):
    total: int
    by_status: dict[AgentRunStatus, int]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    estimated_cost: Decimal | None
