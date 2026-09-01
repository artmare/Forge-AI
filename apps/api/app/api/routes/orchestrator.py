from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_session
from app.orchestration.orchestrator import OrchestratorService
from app.orchestration.runtime_control import RuntimeControlService
from app.schemas.orchestration import OrchestratorHealthResponse, OrchestratorStatusResponse

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def status_response(session: AsyncSession) -> OrchestratorStatusResponse:
    return OrchestratorStatusResponse.model_validate(await RuntimeControlService(session).status())


@router.get("/status", response_model=OrchestratorStatusResponse)
async def orchestrator_status(session: Session) -> OrchestratorStatusResponse:
    return await status_response(session)


@router.post("/pause", response_model=OrchestratorStatusResponse)
async def pause_orchestrator(session: Session) -> OrchestratorStatusResponse:
    await RuntimeControlService(session).set_enabled(False)
    return await status_response(session)


@router.post("/resume", response_model=OrchestratorStatusResponse)
async def resume_orchestrator(session: Session) -> OrchestratorStatusResponse:
    await RuntimeControlService(session).set_enabled(True)
    await OrchestratorService(session).reconcile()
    return await status_response(session)


@router.get(
    "/health",
    response_model=OrchestratorHealthResponse,
    responses={503: {"model": OrchestratorHealthResponse}},
)
async def orchestrator_health(session: Session) -> OrchestratorHealthResponse | JSONResponse:
    status = await status_response(session)
    response = OrchestratorHealthResponse(
        status="healthy" if status.orchestrator_online else "unhealthy",
        autonomy_enabled=status.autonomy_enabled,
        orchestrator_online=status.orchestrator_online,
        last_reconciliation_time=status.last_reconciliation_time,
        active_workers=status.active_workers,
        stale_workers=status.stale_workers,
        queued_jobs=status.queued_jobs,
        running_jobs=status.running_jobs,
    )
    if not status.orchestrator_online:
        return JSONResponse(status_code=503, content=response.model_dump(mode="json"))
    return response
