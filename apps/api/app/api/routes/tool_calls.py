from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infrastructure.database import get_session
from app.schemas.tool_call import (
    RecentToolCallResponse,
    ToolCallResponse,
    ToolCallStatsResponse,
)
from app.services.tool_call import ToolCallService
from app.tool_system.contracts import ToolDefinitionPublic
from app.tool_system.registry import ToolRegistry

router = APIRouter(tags=["tools"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/tools", response_model=list[ToolDefinitionPublic])
async def list_tools() -> list[ToolDefinitionPublic]:
    return ToolRegistry.from_settings(get_settings()).public()


@router.get("/tool-calls", response_model=list[RecentToolCallResponse])
async def list_recent_tool_calls(
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RecentToolCallResponse]:
    rows = await ToolCallService(session).list_recent(offset, limit)
    return [
        RecentToolCallResponse.model_validate(
            {
                **row["call"].__dict__,
                "duration_ms": row["call"].duration_ms,
                "task_title": row["task_title"],
                "agent_name": row["agent_name"],
            }
        )
        for row in rows
    ]


@router.get("/tool-calls/stats", response_model=ToolCallStatsResponse)
async def tool_call_stats(session: Session) -> ToolCallStatsResponse:
    stats = await ToolCallService(session).stats()
    return ToolCallStatsResponse.model_validate(stats.__dict__)


@router.get("/tool-calls/{tool_call_id}", response_model=ToolCallResponse)
async def get_tool_call(tool_call_id: UUID, session: Session) -> ToolCallResponse:
    return ToolCallResponse.model_validate(await ToolCallService(session).get(tool_call_id))


@router.get("/tasks/{task_id}/tool-calls", response_model=list[ToolCallResponse])
async def list_task_tool_calls(
    task_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ToolCallResponse]:
    calls = await ToolCallService(session).list_for_task(task_id, offset, limit)
    return [ToolCallResponse.model_validate(call) for call in calls]


@router.get("/agent-runs/{agent_run_id}/tool-calls", response_model=list[ToolCallResponse])
async def list_agent_run_tool_calls(
    agent_run_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ToolCallResponse]:
    calls = await ToolCallService(session).list_for_agent_run(agent_run_id, offset, limit)
    return [ToolCallResponse.model_validate(call) for call in calls]
