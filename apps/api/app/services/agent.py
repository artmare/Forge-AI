from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.domain.models import Agent
from app.repositories.agent import AgentRepository
from app.repositories.company import CompanyRepository
from app.schemas.agent import AgentCreate, AgentUpdate
from app.services.event_factory import EventFactory


class AgentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.companies = CompanyRepository(session)
        self.agents = AgentRepository(session)
        self.events = EventFactory(session)

    async def create(self, company_id: UUID, payload: AgentCreate) -> Agent:
        if await self.companies.get(company_id) is None:
            raise EntityNotFoundError("Company")
        agent = Agent(company_id=company_id, **payload.model_dump(mode="json"))
        await self.agents.add(agent)
        await self.events.create(
            company_id=company_id,
            agent_id=agent.id,
            event_type="AGENT_CREATED",
            message=f"Agent '{agent.name}' was created.",
            payload={"agent_id": str(agent.id), "role": agent.role},
        )
        await self.session.commit()
        return agent

    async def get(self, agent_id: UUID) -> Agent:
        agent = await self.agents.get(agent_id)
        if agent is None:
            raise EntityNotFoundError("Agent")
        return agent

    async def list_for_company(self, company_id: UUID, offset: int, limit: int) -> list[Agent]:
        if await self.companies.get(company_id) is None:
            raise EntityNotFoundError("Company")
        return await self.agents.list_for_company(company_id, offset, limit)

    async def update(self, agent_id: UUID, payload: AgentUpdate) -> Agent:
        agent = await self.get(agent_id)
        old_status = agent.status
        for field, value in payload.model_dump(exclude_unset=True, mode="json").items():
            setattr(agent, field, value)
        if payload.status is not None and payload.status != old_status:
            await self.events.create(
                company_id=agent.company_id,
                agent_id=agent.id,
                event_type="AGENT_STATUS_CHANGED",
                message=(
                    f"Agent status changed from {old_status.value} to {payload.status.value}."
                ),
                payload={"from": old_status.value, "to": payload.status.value},
            )
        await self.session.commit()
        return agent
