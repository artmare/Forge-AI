from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ExecutionJobStatus
from app.domain.exceptions import EntityNotFoundError
from app.infrastructure.database import get_session
from app.repositories.execution_job import ExecutionJobRepository
from app.schemas.orchestration import ExecutionJobResponse

router = APIRouter(prefix="/execution-jobs", tags=["execution-jobs"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[ExecutionJobResponse])
async def list_execution_jobs(
    session: Session,
    task_id: UUID | None = None,
    agent_id: UUID | None = None,
    worker_id: UUID | None = None,
    status_filter: Annotated[ExecutionJobStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ExecutionJobResponse]:
    jobs = await ExecutionJobRepository(session).list(
        task_id=task_id,
        agent_id=agent_id,
        worker_id=worker_id,
        status=status_filter,
        offset=offset,
        limit=limit,
    )
    return [ExecutionJobResponse.model_validate(job) for job in jobs]


@router.get("/{job_id}", response_model=ExecutionJobResponse)
async def get_execution_job(job_id: UUID, session: Session) -> ExecutionJobResponse:
    job = await ExecutionJobRepository(session).get(job_id)
    if job is None:
        raise EntityNotFoundError("ExecutionJob")
    return ExecutionJobResponse.model_validate(job)
