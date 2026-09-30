from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ModelEscalationResponse(BaseModel):
    from_alias: str
    to_alias: str
    reason_code: str
    reason: str
    objective_signals: list[Any]
    created_at: datetime


class RuntimeEfficiencyResponse(BaseModel):
    task_id: UUID
    model_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost: Decimal
    current_model_alias: str | None
    budget_warning: bool
    stopped_reason: str | None
    limits: dict[str, int | Decimal | None]
    context_bytes_sent: int
    estimated_unchanged_bytes_avoided: int
    context_reduction_ratio: float
    repeated_reads_avoided: int
    duplicate_turns_detected: int
    reused_observations: int
    stale_observation_invalidations: int
    malformed_tool_repairs: int
    stagnation_signals: int
    context_component_bytes: dict[str, int]
    last_useful_action: dict[str, Any]
    deterministic_executions: int
    escalations: list[ModelEscalationResponse]


class MissionEfficiencyResponse(BaseModel):
    mission_id: UUID
    task_count: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost: Decimal
    context_bytes_sent: int
    estimated_unchanged_bytes_avoided: int
    context_reduction_ratio: float
    escalations: int
    tasks: list[RuntimeEfficiencyResponse]


class ProductQAResultResponse(BaseModel):
    id: UUID
    task_id: UUID
    task_run_id: UUID
    iteration: int
    decision: str
    dimensions: list[Any]
    issues: list[Any]
    viewport_contract: list[Any]
    evidence: dict[str, Any]
    screenshot_references: list[Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectKnowledgeResponse(BaseModel):
    project_id: UUID
    architecture_summary: str
    modules: list[Any]
    contracts: list[Any]
    decisions: list[Any]
    constraints: list[Any]
    recent_changes: list[Any]
    updated_at: datetime

    model_config = {"from_attributes": True}


class EfficiencyBenchmarkResponse(BaseModel):
    task_id: UUID
    baseline_context_bytes: int = Field(ge=0)
    optimized_context_bytes: int = Field(ge=0)
    avoided_context_bytes: int = Field(ge=0)
    reduction_ratio: float = Field(ge=0, le=1)
    methodology: str
