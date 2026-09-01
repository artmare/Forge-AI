from datetime import datetime
from decimal import Decimal
from typing import Any, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.domain.enums import MissionStatus, PlanningRunStatus
from app.planning.contracts import PlanProposal, PlanValidationResult
from app.schemas.common import NonEmptyText, ORMResponse

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "db_password",
        "openai_api_key",
        "password",
        "private_key",
        "redis_url",
        "secret",
        "token",
    }
)


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in SENSITIVE_KEYS or _contains_sensitive_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


class MissionCreate(ORMResponse):
    model_config = ConfigDict(extra="forbid")

    company_id: UUID | None = None
    title: NonEmptyText
    goal: NonEmptyText
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    paid_ai_budget: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def reject_secret_fields(self) -> Self:
        if _contains_sensitive_key(self.context) or _contains_sensitive_key(self.constraints):
            raise ValueError("Mission context and constraints must not contain secrets.")
        return self


class MissionProgress(ORMResponse):
    total: int = 0
    done: int = 0
    review: int = 0
    running: int = 0
    queued: int = 0
    blocked: int = 0
    failed: int = 0
    cancelled: int = 0


class MissionResponse(ORMResponse):
    id: UUID
    company_id: UUID | None
    project_id: UUID | None
    title: str
    goal: str
    context: dict[str, Any]
    constraints: dict[str, Any]
    status: MissionStatus
    planning_attempts: int
    max_planning_attempts: int
    created_at: datetime
    updated_at: datetime
    planning_started_at: datetime | None
    planning_completed_at: datetime | None
    execution_started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    failure_reason: str | None
    archived_at: datetime | None
    owns_company: bool
    owns_project: bool
    paid_ai_budget: Decimal
    ai_spend_recorded: Decimal
    project_name: str | None = None
    progress: MissionProgress = Field(default_factory=MissionProgress)


class MissionDeleteRequest(ORMResponse):
    model_config = ConfigDict(extra="forbid")

    confirmation: NonEmptyText


class MissionDeletionResponse(ORMResponse):
    deletion_id: UUID
    mission_id: UUID
    company_id: UUID | None
    project_id: UUID | None
    company_deleted: bool
    project_deleted: bool
    workspace_cleanup_status: str
    workspace_cleanup_error: str | None


class PlanningRunResponse(ORMResponse):
    id: UUID
    mission_id: UUID
    status: PlanningRunStatus
    provider: str
    model_alias: str
    resolved_model: str
    proposal: dict[str, Any] | None
    validation_result: dict[str, Any] | None
    error: dict[str, Any] | None
    provider_attempts: int
    retry_history: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost: Decimal | None
    economic_tier: str | None
    selection_reason: str | None
    required_capabilities: list[str]
    fallback_history: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MissionPlanResponse(ORMResponse):
    planning_run: PlanningRunResponse
    proposal: PlanProposal | None
    validation: PlanValidationResult | None
