from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.development.profile import DevelopmentProfileService
from app.domain.enums import DevelopmentExecutionStatus, TaskStatus
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import AcceptanceVerification, DevelopmentExecution, QAResult, Task
from app.infrastructure.database import get_session
from app.schemas.development import (
    AcceptanceVerificationResponse,
    DevelopmentExecutionResponse,
    DevelopmentProfileResponse,
    DevelopmentSummaryResponse,
    QAResultResponse,
)

router = APIRouter(tags=["development"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/development-executions", response_model=list[DevelopmentExecutionResponse])
async def list_development_executions(
    session: Session,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    agent_id: UUID | None = None,
    status_filter: Annotated[DevelopmentExecutionStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[DevelopmentExecutionResponse]:
    query = select(DevelopmentExecution)
    if task_id is not None:
        query = query.where(DevelopmentExecution.task_id == task_id)
    if project_id is not None:
        query = query.where(DevelopmentExecution.project_id == project_id)
    if agent_id is not None:
        query = query.where(DevelopmentExecution.agent_id == agent_id)
    if status_filter is not None:
        query = query.where(DevelopmentExecution.status == status_filter)
    rows = list(
        await session.scalars(
            query.order_by(DevelopmentExecution.created_at.desc()).offset(offset).limit(limit)
        )
    )
    return [DevelopmentExecutionResponse.model_validate(row) for row in rows]


@router.get("/development-executions/{execution_id}", response_model=DevelopmentExecutionResponse)
async def get_development_execution(
    execution_id: UUID, session: Session
) -> DevelopmentExecutionResponse:
    row = await session.get(DevelopmentExecution, execution_id)
    if row is None:
        raise EntityNotFoundError("DevelopmentExecution")
    return DevelopmentExecutionResponse.model_validate(row)


@router.get(
    "/projects/{project_id}/development-profile",
    response_model=DevelopmentProfileResponse,
)
async def get_development_profile(project_id: UUID, session: Session) -> DevelopmentProfileResponse:
    profile = await DevelopmentProfileService(session).detect(project_id)
    return DevelopmentProfileResponse.model_validate(profile)


@router.get("/tasks/{task_id}/qa-results", response_model=list[QAResultResponse])
async def list_qa_results(task_id: UUID, session: Session) -> list[QAResultResponse]:
    await require_task(session, task_id)
    rows = list(
        await session.scalars(
            select(QAResult)
            .where(QAResult.task_id == task_id)
            .order_by(QAResult.iteration, QAResult.created_at)
        )
    )
    return [QAResultResponse.model_validate(row) for row in rows]


@router.get(
    "/tasks/{task_id}/acceptance-verifications",
    response_model=list[AcceptanceVerificationResponse],
)
async def list_acceptance_verifications(
    task_id: UUID, session: Session
) -> list[AcceptanceVerificationResponse]:
    await require_task(session, task_id)
    rows = list(
        await session.scalars(
            select(AcceptanceVerification)
            .where(AcceptanceVerification.task_id == task_id)
            .order_by(
                AcceptanceVerification.iteration,
                AcceptanceVerification.criterion_index,
            )
        )
    )
    return [AcceptanceVerificationResponse.model_validate(row) for row in rows]


@router.get(
    "/tasks/{task_id}/development-summary",
    response_model=DevelopmentSummaryResponse,
)
async def get_development_summary(task_id: UUID, session: Session) -> DevelopmentSummaryResponse:
    task = await require_task(session, task_id)
    executions = list(
        await session.scalars(
            select(DevelopmentExecution)
            .where(DevelopmentExecution.task_id == task_id)
            .order_by(DevelopmentExecution.created_at)
        )
    )
    qa_results = list(
        await session.scalars(
            select(QAResult).where(QAResult.task_id == task_id).order_by(QAResult.iteration)
        )
    )
    verifications = list(
        await session.scalars(
            select(AcceptanceVerification)
            .where(AcceptanceVerification.task_id == task_id)
            .order_by(AcceptanceVerification.iteration, AcceptanceVerification.criterion_index)
        )
    )
    latest_change = next(
        (
            row.change_summary
            for row in reversed(executions)
            if (row.change_summary or {}).get("total", 0) > 0
        ),
        None,
    )
    stage = {
        TaskStatus.QUEUED: "CODING",
        TaskStatus.IN_PROGRESS: "TESTING_OR_QA",
        TaskStatus.FIX_REQUIRED: "FIX_REQUIRED",
        TaskStatus.REVIEW: "HUMAN_REVIEW",
        TaskStatus.DONE: "APPROVED",
        TaskStatus.FAILED: "FAILED",
        TaskStatus.CANCELLED: "CANCELLED",
        TaskStatus.CREATED: "PLANNED",
    }[task.status]
    return DevelopmentSummaryResponse(
        task_id=task.id,
        current_stage=stage,
        latest_change_summary=latest_change,
        executions=[DevelopmentExecutionResponse.model_validate(row) for row in executions],
        qa_results=[QAResultResponse.model_validate(row) for row in qa_results],
        acceptance_verifications=[
            AcceptanceVerificationResponse.model_validate(row) for row in verifications
        ],
    )


async def require_task(session: AsyncSession, task_id: UUID) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise EntityNotFoundError("Task")
    return task
