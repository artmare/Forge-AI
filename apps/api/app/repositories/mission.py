from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MissionStatus
from app.domain.models import Mission


class MissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, mission: Mission) -> Mission:
        self.session.add(mission)
        await self.session.flush()
        return mission

    async def get(self, mission_id: UUID) -> Mission | None:
        return await self.session.get(Mission, mission_id)

    async def get_for_update(self, mission_id: UUID) -> Mission | None:
        return await self.session.scalar(
            select(Mission).where(Mission.id == mission_id).with_for_update()
        )

    async def list(
        self,
        *,
        company_id: UUID | None = None,
        status: MissionStatus | None = None,
        archive: Literal["active", "archived", "all"] = "active",
        offset: int = 0,
        limit: int = 100,
    ) -> list[Mission]:
        statement = select(Mission)
        if company_id is not None:
            statement = statement.where(Mission.company_id == company_id)
        if status is not None:
            statement = statement.where(Mission.status == status)
        if archive == "active":
            statement = statement.where(Mission.archived_at.is_(None))
        elif archive == "archived":
            statement = statement.where(Mission.archived_at.is_not(None))
        rows = await self.session.scalars(
            statement.order_by(Mission.created_at.desc(), Mission.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows)
