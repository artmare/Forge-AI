from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import get_session
from app.schemas.artifact import ArtifactContentResponse, ArtifactMetadataResponse
from app.schemas.dependency import ProjectGraphResponse
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.artifact import ArtifactService
from app.services.project import ProjectService
from app.services.task_dependencies import TaskDependencyService

router = APIRouter(tags=["projects"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/companies/{company_id}/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    company_id: UUID, payload: ProjectCreate, session: Session
) -> ProjectResponse:
    return ProjectResponse.model_validate(await ProjectService(session).create(company_id, payload))


@router.get("/companies/{company_id}/projects", response_model=list[ProjectResponse])
async def list_projects(
    company_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ProjectResponse]:
    projects = await ProjectService(session).list_for_company(company_id, offset, limit)
    return [ProjectResponse.model_validate(project) for project in projects]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: UUID, session: Session) -> ProjectResponse:
    return ProjectResponse.model_validate(await ProjectService(session).get(project_id))


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID, payload: ProjectUpdate, session: Session
) -> ProjectResponse:
    return ProjectResponse.model_validate(await ProjectService(session).update(project_id, payload))


@router.get("/projects/{project_id}/graph", response_model=ProjectGraphResponse)
async def get_project_graph(project_id: UUID, session: Session) -> ProjectGraphResponse:
    return await TaskDependencyService(session).graph(project_id)


@router.get(
    "/projects/{project_id}/artifacts",
    response_model=list[ArtifactMetadataResponse],
)
async def list_project_artifacts(
    project_id: UUID, session: Session
) -> list[ArtifactMetadataResponse]:
    return await ArtifactService(session).list(project_id)


@router.get(
    "/projects/{project_id}/artifacts/content",
    response_model=ArtifactContentResponse,
)
async def read_project_artifact(
    project_id: UUID,
    session: Session,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
) -> ArtifactContentResponse:
    return await ArtifactService(session).read(project_id, path)
