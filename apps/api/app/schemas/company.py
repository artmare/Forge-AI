from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.domain.enums import CompanyStatus
from app.schemas.common import NonEmptyText, ORMResponse, PartialUpdate, Slug


class CompanyCreate(ORMResponse):
    name: NonEmptyText
    slug: Slug
    goal: NonEmptyText
    description: str | None = None
    capital_available: Decimal = Field(default=Decimal("0"), ge=0)
    revenue_recorded: Decimal = Field(default=Decimal("0"), ge=0)
    paid_ai_budget: Decimal = Field(default=Decimal("0"), ge=0)


class CompanyUpdate(PartialUpdate):
    non_nullable_fields = frozenset({"name", "slug", "goal", "status"})

    name: NonEmptyText | None = None
    slug: Slug | None = None
    goal: NonEmptyText | None = None
    status: CompanyStatus | None = None
    description: str | None = None
    capital_available: Decimal | None = Field(default=None, ge=0)
    revenue_recorded: Decimal | None = Field(default=None, ge=0)
    paid_ai_budget: Decimal | None = Field(default=None, ge=0)


class CompanyResponse(ORMResponse):
    id: UUID
    name: str
    slug: str
    goal: str
    status: CompanyStatus
    description: str | None
    capital_available: Decimal
    revenue_recorded: Decimal
    paid_ai_budget: Decimal
    ai_spend_recorded: Decimal
    created_at: datetime
    updated_at: datetime
