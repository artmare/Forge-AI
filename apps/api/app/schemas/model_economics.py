from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.schemas.common import ORMResponse


class ModelCallResponse(ORMResponse):
    id: UUID
    company_id: UUID | None
    mission_id: UUID | None
    task_id: UUID | None
    provider: str
    model_id: str
    economic_tier: str
    paid: bool
    status: str
    agent_role: str
    selection_reason: str
    required_capabilities: list[str]
    fallback_from_provider: str | None
    fallback_from_model: str | None
    fallback_reason: str | None
    estimated_cost: Decimal
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None


class EconomicsSummaryResponse(ORMResponse):
    scope: str
    scope_id: UUID
    paid_ai_budget: Decimal
    ai_spend_recorded: Decimal
    paid_ai_budget_remaining: Decimal
    capital_available: Decimal | None = None
    revenue_recorded: Decimal | None = None
    reinvestable_capital: Decimal | None = None
    total_calls: int
    calls_by_tier: dict[str, int]
    fallback_count: int
    escalation_count: int
    recent_calls: list[ModelCallResponse]
