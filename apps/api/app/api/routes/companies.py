from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_session
from app.schemas.company import CompanyCreate, CompanyResponse, CompanyUpdate
from app.services.company import CompanyService

router = APIRouter(prefix="/companies", tags=["companies"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(payload: CompanyCreate, session: Session) -> CompanyResponse:
    return CompanyResponse.model_validate(await CompanyService(session).create(payload))


@router.get("", response_model=list[CompanyResponse])
async def list_companies(
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[CompanyResponse]:
    companies = await CompanyService(session).list(offset, limit)
    return [CompanyResponse.model_validate(company) for company in companies]


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(company_id: UUID, session: Session) -> CompanyResponse:
    return CompanyResponse.model_validate(await CompanyService(session).get(company_id))


@router.patch("/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: UUID, payload: CompanyUpdate, session: Session
) -> CompanyResponse:
    return CompanyResponse.model_validate(await CompanyService(session).update(company_id, payload))
