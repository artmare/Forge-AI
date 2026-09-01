from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskRunStatus, TaskStatus
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import (
    AcceptanceVerification,
    DevelopmentExecution,
    Project,
    QAResult,
    Task,
    TaskDependency,
    TaskRun,
    ToolCall,
)
from app.planning.dependency_resolver import DependencyResolver
from app.repositories.task_review import TaskReviewRepository
from app.schemas.dependency import (
    DependencyTaskSummary,
    GraphToolCall,
    ProjectGraphEdge,
    ProjectGraphNode,
    ProjectGraphResponse,
    TaskDependencyResponse,
)
from app.schemas.development import (
    AcceptanceVerificationResponse,
    DevelopmentExecutionResponse,
    QAResultResponse,
)
from app.schemas.task_review import TaskReviewResponse
from app.services.task_review import TaskReviewService


class TaskDependencyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resolver = DependencyResolver(session)
        self.reviews = TaskReviewRepository(session)

    async def dependencies(self, task_id: UUID) -> list[TaskDependencyResponse]:
        await self._require_task(task_id)
        rows = list(
            await self.session.execute(
                select(TaskDependency, Task)
                .join(Task, Task.id == TaskDependency.depends_on_task_id)
                .where(TaskDependency.task_id == task_id)
                .order_by(TaskDependency.created_at, TaskDependency.id)
            )
        )
        return [
            TaskDependencyResponse(
                id=edge.id,
                task_id=edge.task_id,
                depends_on_task_id=edge.depends_on_task_id,
                created_at=edge.created_at,
                task=DependencyTaskSummary(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    priority=task.priority,
                    assigned_agent_id=task.assigned_agent_id,
                    blocked_reason=await self.resolver.blocked_reason(task.id),
                ),
            )
            for edge, task in rows
        ]

    async def dependents(self, task_id: UUID) -> list[TaskDependencyResponse]:
        await self._require_task(task_id)
        rows = list(
            await self.session.execute(
                select(TaskDependency, Task)
                .join(Task, Task.id == TaskDependency.task_id)
                .where(TaskDependency.depends_on_task_id == task_id)
                .order_by(TaskDependency.created_at, TaskDependency.id)
            )
        )
        return [
            TaskDependencyResponse(
                id=edge.id,
                task_id=edge.task_id,
                depends_on_task_id=edge.depends_on_task_id,
                created_at=edge.created_at,
                task=DependencyTaskSummary(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    priority=task.priority,
                    assigned_agent_id=task.assigned_agent_id,
                    blocked_reason=await self.resolver.blocked_reason(task.id),
                ),
            )
            for edge, task in rows
        ]

    async def approve(self, task_id: UUID) -> Task:
        return await TaskReviewService(self.session).approve(task_id)

    async def request_fix(self, task_id: UUID, feedback: str) -> Task:
        return await TaskReviewService(self.session).request_fix(task_id, feedback)

    async def graph(self, project_id: UUID) -> ProjectGraphResponse:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise EntityNotFoundError("Project")
        tasks = list(
            await self.session.scalars(
                select(Task).where(Task.project_id == project_id).order_by(Task.created_at, Task.id)
            )
        )
        edges = list(
            await self.session.scalars(
                select(TaskDependency)
                .join(Task, Task.id == TaskDependency.task_id)
                .where(Task.project_id == project_id)
                .order_by(TaskDependency.created_at, TaskDependency.id)
            )
        )
        nodes: list[ProjectGraphNode] = []
        for task in tasks:
            result = await self.session.scalar(
                select(TaskRun.output)
                .where(
                    TaskRun.task_id == task.id,
                    TaskRun.status == TaskRunStatus.SUCCEEDED,
                )
                .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
                .limit(1)
            )
            blocked_reason = await self.resolver.blocked_reason(task.id)
            tool_calls = list(
                await self.session.scalars(
                    select(ToolCall)
                    .where(ToolCall.task_id == task.id)
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            )
            reviews = await self.reviews.list_for_task(task.id)
            development_executions = list(
                await self.session.scalars(
                    select(DevelopmentExecution)
                    .where(DevelopmentExecution.task_id == task.id)
                    .order_by(DevelopmentExecution.created_at, DevelopmentExecution.id)
                )
            )
            qa_results = list(
                await self.session.scalars(
                    select(QAResult)
                    .where(QAResult.task_id == task.id)
                    .order_by(QAResult.iteration, QAResult.created_at)
                )
            )
            verifications = list(
                await self.session.scalars(
                    select(AcceptanceVerification)
                    .where(AcceptanceVerification.task_id == task.id)
                    .order_by(
                        AcceptanceVerification.iteration,
                        AcceptanceVerification.criterion_index,
                    )
                )
            )
            nodes.append(
                ProjectGraphNode(
                    id=task.id,
                    title=task.title,
                    description=task.description,
                    kind=task.kind,
                    status=task.status,
                    priority=task.priority,
                    assigned_agent_id=task.assigned_agent_id,
                    acceptance_criteria=task.acceptance_criteria,
                    iteration=task.iteration,
                    max_iterations=task.max_iterations,
                    state=self._graph_state(task.status, blocked_reason),
                    ready=task.status == TaskStatus.QUEUED,
                    blocked_reason=blocked_reason,
                    result=result,
                    tool_calls=[
                        GraphToolCall(
                            id=call.id,
                            tool_name=call.tool_name,
                            status=call.status,
                            result=call.result,
                            error=call.error,
                        )
                        for call in tool_calls
                    ],
                    reviews=[TaskReviewResponse.model_validate(review) for review in reviews],
                    development_executions=[
                        DevelopmentExecutionResponse.model_validate(row)
                        for row in development_executions
                    ],
                    qa_results=[QAResultResponse.model_validate(row) for row in qa_results],
                    acceptance_verifications=[
                        AcceptanceVerificationResponse.model_validate(row) for row in verifications
                    ],
                )
            )
        return ProjectGraphResponse(
            project_id=project.id,
            nodes=nodes,
            edges=[
                ProjectGraphEdge(
                    id=edge.id,
                    task_id=edge.task_id,
                    depends_on_task_id=edge.depends_on_task_id,
                )
                for edge in edges
            ],
        )

    async def _require_task(self, task_id: UUID) -> Task:
        task = await self.session.get(Task, task_id)
        if task is None:
            raise EntityNotFoundError("Task")
        return task

    @staticmethod
    def _graph_state(status: TaskStatus, blocked_reason: str | None) -> str:
        if blocked_reason is not None:
            return "blocked"
        return {
            TaskStatus.CREATED: "created",
            TaskStatus.QUEUED: "ready",
            TaskStatus.IN_PROGRESS: "running",
            TaskStatus.REVIEW: "review",
            TaskStatus.FIX_REQUIRED: "fix_required",
            TaskStatus.DONE: "done",
            TaskStatus.FAILED: "failed",
            TaskStatus.CANCELLED: "cancelled",
        }[status]
