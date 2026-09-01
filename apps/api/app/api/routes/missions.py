from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MissionStatus, PlanningRunStatus
from app.domain.exceptions import MissionPlanningError
from app.infrastructure.database import get_session
from app.planning.company_factory import CompanyFactory
from app.planning.contracts import PlanProposal, PlanValidationResult
from app.planning.mission_planner import MissionPlanner
from app.schemas.mission import (
    MissionCreate,
    MissionDeleteRequest,
    MissionDeletionResponse,
    MissionPlanResponse,
    MissionResponse,
    PlanningRunResponse,
)
from app.services.mission import MissionService
from app.services.mission_lifecycle import MissionLifecycleService

router = APIRouter(prefix="/missions", tags=["missions"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def _response(service: MissionService, mission) -> MissionResponse:  # type: ignore[no-untyped-def]
    response = MissionResponse.model_validate(mission)
    return response.model_copy(
        update={
            "progress": await service.progress(mission),
            "project_name": await service.project_name(mission),
        }
    )


@router.post("", response_model=MissionResponse, status_code=status.HTTP_201_CREATED)
async def create_mission(payload: MissionCreate, session: Session) -> MissionResponse:
    service = MissionService(session)
    return await _response(service, await service.create(payload))


@router.get("", response_model=list[MissionResponse])
async def list_missions(
    session: Session,
    company_id: UUID | None = None,
    status_filter: Annotated[MissionStatus | None, Query(alias="status")] = None,
    archive: Annotated[Literal["active", "archived", "all"], Query()] = "active",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[MissionResponse]:
    service = MissionService(session)
    missions = await service.list(
        company_id=company_id,
        status=status_filter,
        archive=archive,
        offset=offset,
        limit=limit,
    )
    return [await _response(service, mission) for mission in missions]


@router.get("/{mission_id}", response_model=MissionResponse)
async def get_mission(mission_id: UUID, session: Session) -> MissionResponse:
    service = MissionService(session)
    return await _response(service, await service.get(mission_id))


@router.post("/{mission_id}/plan", response_model=PlanningRunResponse)
async def plan_mission(mission_id: UUID, session: Session) -> PlanningRunResponse:
    run = await MissionPlanner(session).plan(mission_id)
    if run.status == PlanningRunStatus.INVALID:
        error = run.error or {
            "code": "INVALID_PLAN",
            "message": "Plan validation failed.",
            "retryable": False,
        }
        raise MissionPlanningError(
            str(error["code"]),
            str(error["message"]),
            422,
            details={
                **error,
                "planning_run_id": str(run.id),
                "mission_id": str(run.mission_id),
            },
        )
    return PlanningRunResponse.model_validate(run)


@router.get("/{mission_id}/plan", response_model=MissionPlanResponse)
async def get_mission_plan(mission_id: UUID, session: Session) -> MissionPlanResponse:
    run = await MissionService(session).latest_plan(mission_id)
    return MissionPlanResponse(
        planning_run=PlanningRunResponse.model_validate(run),
        proposal=PlanProposal.model_validate(run.proposal) if run.proposal else None,
        validation=(
            PlanValidationResult.model_validate(run.validation_result)
            if run.validation_result
            else None
        ),
    )


@router.post("/{mission_id}/activate", response_model=MissionResponse)
async def activate_mission(mission_id: UUID, session: Session) -> MissionResponse:
    mission = await CompanyFactory(session).activate(mission_id)
    return await _response(MissionService(session), mission)


@router.post("/{mission_id}/cancel", response_model=MissionResponse)
async def cancel_mission(mission_id: UUID, session: Session) -> MissionResponse:
    service = MissionService(session)
    return await _response(service, await service.cancel(mission_id))


@router.post("/{mission_id}/archive", response_model=MissionResponse)
async def archive_mission(mission_id: UUID, session: Session) -> MissionResponse:
    service = MissionService(session)
    mission = await MissionLifecycleService(session).archive(mission_id)
    return await _response(service, mission)


@router.post("/{mission_id}/restore", response_model=MissionResponse)
async def restore_mission(mission_id: UUID, session: Session) -> MissionResponse:
    service = MissionService(session)
    mission = await MissionLifecycleService(session).restore(mission_id)
    return await _response(service, mission)


@router.delete("/{mission_id}", response_model=MissionDeletionResponse)
async def delete_mission(
    mission_id: UUID, payload: MissionDeleteRequest, session: Session
) -> MissionDeletionResponse:
    return await MissionLifecycleService(session).delete(mission_id, payload.confirmation)
