from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_session
from app.schemas.agent_run import (
    AgentRunResponse,
    AgentRunStatsResponse,
    RecentAgentRunResponse,
)
from app.services.agent_run import AgentRunService

router = APIRouter(tags=["agent-runs"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/agent-runs", response_model=list[RecentAgentRunResponse])
async def list_recent_agent_runs(
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RecentAgentRunResponse]:
    rows = await AgentRunService(session).list_recent(offset, limit)
    return [
        RecentAgentRunResponse.model_validate(
            {
                **row["run"].__dict__,
                "task_title": row["task_title"],
                "agent_name": row["agent_name"],
                "agent_role": row["agent_role"],
            }
        )
        for row in rows
    ]


@router.get("/agent-runs/stats", response_model=AgentRunStatsResponse)
async def agent_run_stats(session: Session, agent_id: UUID | None = None) -> AgentRunStatsResponse:
    stats = await AgentRunService(session).stats(agent_id)
    return AgentRunStatsResponse.model_validate(stats.__dict__)


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(run_id: UUID, session: Session) -> AgentRunResponse:
    return AgentRunResponse.model_validate(await AgentRunService(session).get(run_id))


@router.get("/tasks/{task_id}/agent-runs", response_model=list[AgentRunResponse])
async def list_task_agent_runs(
    task_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[AgentRunResponse]:
    runs = await AgentRunService(session).list_for_task(task_id, offset, limit)
    return [AgentRunResponse.model_validate(run) for run in runs]
