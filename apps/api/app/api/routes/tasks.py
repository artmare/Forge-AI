from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.efficiency import EfficientRuntimeService
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import get_settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import TaskKind, TaskStatus
from app.domain.models import Task
from app.infrastructure.database import get_session
from app.orchestration.manual_execution import ManualExecutionGuard
from app.schemas.agent_run import AgentRunResponse, ExecuteTaskRequest
from app.schemas.dependency import TaskDependencyResponse
from app.schemas.task import (
    TaskBudgetResumeRequest,
    TaskCreate,
    TaskResponse,
    TaskTransitionRequest,
    TaskUpdate,
)
from app.schemas.task_inspection import TaskInspectionResponse
from app.schemas.task_review import TaskFixRequest, TaskReviewResponse
from app.schemas.task_run import TaskRunResponse
from app.services.task import TaskService
from app.services.task_dependencies import TaskDependencyService
from app.services.task_inspection import TaskInspectionService
from app.services.task_review import TaskReviewService
from app.services.task_state_machine import TaskStateMachine

router = APIRouter(prefix="/tasks", tags=["tasks"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: Session) -> TaskResponse:
    return TaskResponse.model_validate(await TaskService(session).create(payload))


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    session: Session,
    company_id: UUID | None = None,
    project_id: UUID | None = None,
    assigned_agent_id: UUID | None = None,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[TaskResponse]:
    tasks = await TaskService(session).list(
        company_id=company_id,
        project_id=project_id,
        assigned_agent_id=assigned_agent_id,
        status=status_filter,
        offset=offset,
        limit=limit,
    )
    return [TaskResponse.model_validate(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, session: Session) -> TaskResponse:
    return TaskResponse.model_validate(await TaskService(session).get(task_id))


@router.get("/{task_id}/inspection", response_model=TaskInspectionResponse)
async def inspect_task(task_id: UUID, session: Session) -> TaskInspectionResponse:
    return await TaskInspectionService(session).get(task_id)


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(task_id: UUID, payload: TaskUpdate, session: Session) -> TaskResponse:
    return TaskResponse.model_validate(await TaskService(session).update(task_id, payload))


@router.post("/{task_id}/transition", response_model=TaskResponse)
async def transition_task(
    task_id: UUID, payload: TaskTransitionRequest, session: Session
) -> TaskResponse:
    task = await TaskStateMachine(session).transition(
        task_id, payload.target_status, payload.reason
    )
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/resume-input-budget", response_model=TaskResponse)
async def resume_input_budget(
    task_id: UUID, payload: TaskBudgetResumeRequest, session: Session
) -> TaskResponse:
    task = await EfficientRuntimeService(session, get_settings()).approve_input_budget_resume(
        task_id, payload.additional_input_tokens
    )
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/resume-context", response_model=TaskResponse)
async def resume_context(task_id: UUID, session: Session) -> TaskResponse:
    task = await EfficientRuntimeService(session, get_settings()).approve_context_resume(task_id)
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/execute", response_model=AgentRunResponse)
async def execute_task(
    task_id: UUID, payload: ExecuteTaskRequest, session: Session
) -> AgentRunResponse:
    guard = ManualExecutionGuard(session)
    agent_id = await guard.acquire(task_id)
    try:
        task = await session.get(Task, task_id)
        is_development = task is not None and task.kind == TaskKind.DEVELOPMENT
        if is_development:
            await ProjectBootstrapService(session).ensure_task(task_id, checkpoint=True)
        run = await AgentRuntime(session).execute(
            task_id, payload.model_alias, defer_review=is_development
        )
        if is_development:
            await DevelopmentWorkflowService(session).finalize(task_id, run.task_run_id)
        return AgentRunResponse.model_validate(run)
    finally:
        await guard.release(agent_id)


@router.get("/{task_id}/runs", response_model=list[TaskRunResponse])
async def list_task_runs(
    task_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[TaskRunResponse]:
    runs = await TaskStateMachine(session).list_runs(task_id, offset, limit)
    return [TaskRunResponse.model_validate(run) for run in runs]


@router.get("/{task_id}/dependencies", response_model=list[TaskDependencyResponse])
async def list_task_dependencies(task_id: UUID, session: Session) -> list[TaskDependencyResponse]:
    return await TaskDependencyService(session).dependencies(task_id)


@router.get("/{task_id}/dependents", response_model=list[TaskDependencyResponse])
async def list_task_dependents(task_id: UUID, session: Session) -> list[TaskDependencyResponse]:
    return await TaskDependencyService(session).dependents(task_id)


@router.post("/{task_id}/approve", response_model=TaskResponse)
async def approve_task(task_id: UUID, session: Session) -> TaskResponse:
    return TaskResponse.model_validate(await TaskReviewService(session).approve(task_id))


@router.post("/{task_id}/request-fix", response_model=TaskResponse)
async def request_task_fix(
    task_id: UUID, payload: TaskFixRequest, session: Session
) -> TaskResponse:
    return TaskResponse.model_validate(
        await TaskReviewService(session).request_fix(task_id, payload.feedback)
    )


@router.get("/{task_id}/reviews", response_model=list[TaskReviewResponse])
async def list_task_reviews(
    task_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[TaskReviewResponse]:
    reviews = await TaskReviewService(session).list(task_id, offset, limit)
    return [TaskReviewResponse.model_validate(review) for review in reviews]
