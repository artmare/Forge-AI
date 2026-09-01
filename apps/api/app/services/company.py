from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import DuplicateSlugError, EntityNotFoundError
from app.domain.models import Company
from app.repositories.company import CompanyRepository
from app.schemas.company import CompanyCreate, CompanyUpdate
from app.services.event_factory import EventFactory


class CompanyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.companies = CompanyRepository(session)
        self.events = EventFactory(session)

    async def create(self, payload: CompanyCreate) -> Company:
        company = Company(**payload.model_dump())
        try:
            await self.companies.add(company)
            await self.events.create(
                company_id=company.id,
                event_type="COMPANY_CREATED",
                message=f"Company '{company.name}' was created.",
                payload={"company_id": str(company.id)},
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateSlugError() from exc
        return company

    async def get(self, company_id: UUID) -> Company:
        company = await self.companies.get(company_id)
        if company is None:
            raise EntityNotFoundError("Company")
        return company

    async def list(self, offset: int, limit: int) -> list[Company]:
        return await self.companies.list(offset, limit)

    async def update(self, company_id: UUID, payload: CompanyUpdate) -> Company:
        company = await self.get(company_id)
        old_status = company.status
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(company, field, value)
        if payload.status is not None and payload.status != old_status:
            await self.events.create(
                company_id=company.id,
                event_type="COMPANY_STATUS_CHANGED",
                message=(
                    f"Company status changed from {old_status.value} to {payload.status.value}."
                ),
                payload={"from": old_status.value, "to": payload.status.value},
            )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise DuplicateSlugError() from exc
        return company
