from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.health import collect_health
from app.core.config import get_settings
from app.domain.enums import TaskStatus
from app.infrastructure.database import get_session
from app.repositories.system import SystemRepository

router = APIRouter(tags=["system"])


class SystemResponse(BaseModel):
    name: str
    version: str
    status: Literal["healthy", "unhealthy"]
    companies: int
    agents: int
    tasks: int
    task_states: dict[TaskStatus, int]


@router.get("/system", response_model=SystemResponse)
async def system(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SystemResponse:
    health = await collect_health()
    counts = await SystemRepository(session).counts()
    return SystemResponse(
        name="Forge",
        version=get_settings().app_version,
        status=health.status,
        companies=counts.companies,
        agents=counts.agents,
        tasks=counts.tasks,
        task_states=counts.task_states,
    )
