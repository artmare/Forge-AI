from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Agent


class AgentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, agent: Agent) -> Agent:
        self.session.add(agent)
        await self.session.flush()
        return agent

    async def get(self, agent_id: UUID) -> Agent | None:
        return await self.session.get(Agent, agent_id)

    async def get_current(self, agent_id: UUID) -> Agent | None:
        return await self.session.scalar(
            select(Agent).where(Agent.id == agent_id).execution_options(populate_existing=True)
        )

    async def list_for_company(
        self, company_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[Agent]:
        result = await self.session.scalars(
            select(Agent)
            .where(Agent.company_id == company_id)
            .order_by(Agent.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)
