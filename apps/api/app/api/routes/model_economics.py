from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.domain.models import Company, Mission, ModelCallRecord
from app.infrastructure.database import get_session
from app.schemas.model_economics import EconomicsSummaryResponse, ModelCallResponse

router = APIRouter(prefix="/economics", tags=["economics"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def _summary(
    session: Session,
    *,
    scope: str,
    scope_id: UUID,
    company: Company | None,
    mission: Mission | None,
) -> EconomicsSummaryResponse:
    field = ModelCallRecord.company_id if scope == "COMPANY" else ModelCallRecord.mission_id
    records = list(
        await session.scalars(
            select(ModelCallRecord)
            .where(field == scope_id)
            .order_by(ModelCallRecord.created_at.desc(), ModelCallRecord.id.desc())
            .limit(50)
        )
    )
    grouped = await session.execute(
        select(ModelCallRecord.economic_tier, func.count(ModelCallRecord.id))
        .where(field == scope_id)
        .group_by(ModelCallRecord.economic_tier)
    )
    calls_by_tier = {tier: int(count) for tier, count in grouped}
    owner = company if scope == "COMPANY" else mission
    assert owner is not None
    budget = owner.paid_ai_budget
    spend = owner.ai_spend_recorded
    remaining = max(budget - spend, Decimal("0"))
    capital = company.capital_available if company else None
    revenue = company.revenue_recorded if company else None
    reinvestable = (
        max(capital + revenue - company.ai_spend_recorded, Decimal("0"))
        if company is not None
        else None
    )
    return EconomicsSummaryResponse(
        scope=scope,
        scope_id=scope_id,
        paid_ai_budget=budget,
        ai_spend_recorded=spend,
        paid_ai_budget_remaining=remaining,
        capital_available=capital,
        revenue_recorded=revenue,
        reinvestable_capital=reinvestable,
        total_calls=sum(calls_by_tier.values()),
        calls_by_tier=calls_by_tier,
        fallback_count=sum(1 for item in records if item.fallback_from_provider),
        escalation_count=sum(1 for item in records if item.economic_tier == "PREMIUM"),
        recent_calls=[ModelCallResponse.model_validate(item) for item in records],
    )


@router.get("/companies/{company_id}", response_model=EconomicsSummaryResponse)
async def company_economics(company_id: UUID, session: Session) -> EconomicsSummaryResponse:
    company = await session.get(Company, company_id)
    if company is None:
        raise EntityNotFoundError("Company")
    return await _summary(
        session,
        scope="COMPANY",
        scope_id=company.id,
        company=company,
        mission=None,
    )


@router.get("/missions/{mission_id}", response_model=EconomicsSummaryResponse)
async def mission_economics(mission_id: UUID, session: Session) -> EconomicsSummaryResponse:
    mission = await session.get(Mission, mission_id)
    if mission is None:
        raise EntityNotFoundError("Mission")
    company = await session.get(Company, mission.company_id) if mission.company_id else None
    return await _summary(
        session,
        scope="MISSION",
        scope_id=mission.id,
        company=company,
        mission=mission,
    )
