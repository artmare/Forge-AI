from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_session
from app.schemas.agent import AgentCreate, AgentResponse, AgentUpdate
from app.services.agent import AgentService

router = APIRouter(tags=["agents"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/companies/{company_id}/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent(company_id: UUID, payload: AgentCreate, session: Session) -> AgentResponse:
    return AgentResponse.model_validate(await AgentService(session).create(company_id, payload))


@router.get("/companies/{company_id}/agents", response_model=list[AgentResponse])
async def list_agents(
    company_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[AgentResponse]:
    agents = await AgentService(session).list_for_company(company_id, offset, limit)
    return [AgentResponse.model_validate(agent) for agent in agents]


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID, session: Session) -> AgentResponse:
    return AgentResponse.model_validate(await AgentService(session).get(agent_id))


@router.patch("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: UUID, payload: AgentUpdate, session: Session) -> AgentResponse:
    return AgentResponse.model_validate(await AgentService(session).update(agent_id, payload))
