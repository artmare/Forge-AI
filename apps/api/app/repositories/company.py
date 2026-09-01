from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Company


class CompanyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, company: Company) -> Company:
        self.session.add(company)
        await self.session.flush()
        return company

    async def get(self, company_id: UUID) -> Company | None:
        return await self.session.get(Company, company_id)

    async def get_by_slug(self, slug: str) -> Company | None:
        return await self.session.scalar(select(Company).where(Company.slug == slug))

    async def list(self, offset: int = 0, limit: int = 100) -> list[Company]:
        result = await self.session.scalars(
            select(Company).order_by(Company.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result)
