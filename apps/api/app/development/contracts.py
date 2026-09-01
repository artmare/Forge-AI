from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import (
    DevelopmentAction,
    DevelopmentExecutionStatus,
    DevelopmentProjectType,
    ToolRiskLevel,
)

FORBIDDEN_ARGUMENT_TOKENS = ("..", "|", ">", "<", "&&", "||", "$", "`", "\x00", "\n", "\r")


class SafeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = Field(default=None, max_length=512)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("\\", "/")
        if not normalized or normalized.startswith("/") or ":" in normalized[:3]:
            raise ValueError("target must be a non-empty relative path")
        if any(token in normalized for token in FORBIDDEN_ARGUMENT_TOKENS):
            raise ValueError("target contains prohibited command or path syntax")
        return normalized


class RunnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    action: DevelopmentAction
    workspace_relative: str = Field(pattern=r"^[0-9a-f-]{36}/[0-9a-f-]{36}$")
    safe_arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(gt=0, le=600)
    output_limit_bytes: int = Field(gt=0, le=1_000_000)
    task_id: UUID


class RunnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: DevelopmentExecutionStatus
    exit_code: int | None = None
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    truncated: bool = False
    duration_ms: float = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CommandDefinition:
    action: DevelopmentAction
    description: str
    project_types: frozenset[DevelopmentProjectType]
    permission_required: str
    timeout_seconds: int
    network_enabled: bool
    output_limit_bytes: int
    risk_level: ToolRiskLevel
    enabled: bool = True
    accepts_target: bool = False

    def validate_arguments(self, raw: dict[str, Any]) -> dict[str, Any]:
        if self.accepts_target:
            target = SafeTarget.model_validate(raw).target
            return {"target": target} if target is not None else {}
        if raw:
            raise ValueError(f"{self.action.value} does not accept arguments")
        return {}
