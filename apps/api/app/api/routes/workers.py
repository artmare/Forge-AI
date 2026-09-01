from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.infrastructure.database import get_session
from app.repositories.worker_node import WorkerNodeRepository
from app.schemas.orchestration import WorkerResponse

router = APIRouter(prefix="/workers", tags=["workers"])
Session = Annotated[AsyncSession, Depends(get_session)]


def worker_response(row) -> WorkerResponse:  # type: ignore[no-untyped-def]
    worker, active_jobs = row
    return WorkerResponse(
        id=worker.id,
        worker_key=worker.worker_key,
        status=worker.status,
        concurrency=worker.concurrency,
        started_at=worker.started_at,
        last_heartbeat_at=worker.last_heartbeat_at,
        stopped_at=worker.stopped_at,
        active_jobs=active_jobs,
        created_at=worker.created_at,
        updated_at=worker.updated_at,
    )


@router.get("", response_model=list[WorkerResponse])
async def list_workers(
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[WorkerResponse]:
    return [worker_response(row) for row in await WorkerNodeRepository(session).list(offset, limit)]


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(worker_id: UUID, session: Session) -> WorkerResponse:
    row = await WorkerNodeRepository(session).get_with_active_count(worker_id)
    if row is None:
        raise EntityNotFoundError("Worker")
    return worker_response(row)
