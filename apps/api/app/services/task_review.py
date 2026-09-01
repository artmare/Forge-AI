from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentRunStatus, TaskReviewDecision, TaskStatus
from app.domain.exceptions import EntityNotFoundError, TaskReviewConflictError
from app.domain.models import AgentRun, Task, TaskReview, TaskRun
from app.planning.dependency_resolver import DependencyResolver
from app.repositories.task import TaskRepository
from app.repositories.task_review import TaskReviewRepository
from app.services.event_factory import EventFactory
from app.services.project_knowledge import ProjectKnowledgeService
from app.services.task_state_machine import TaskStateMachine


class TaskReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tasks = TaskRepository(session)
        self.reviews = TaskReviewRepository(session)
        self.events = EventFactory(session)
        self.resolver = DependencyResolver(session)

    async def list(self, task_id: UUID, offset: int = 0, limit: int = 100) -> list[TaskReview]:
        if await self.tasks.get(task_id) is None:
            raise EntityNotFoundError("Task")
        return await self.reviews.list_for_task(task_id, offset, limit)

    async def approve(self, task_id: UUID) -> Task:
        try:
            task = await self._lock_reviewable(task_id)
            review = await self._record(task, TaskReviewDecision.APPROVED, None)
            task = await TaskStateMachine(self.session).transition(
                task.id,
                TaskStatus.DONE,
                "Task output was explicitly approved.",
                commit=False,
            )
            await self._review_event(task, review, "TASK_REVIEW_APPROVED")
            await ProjectKnowledgeService(self.session).compact_approved_task(task)
            await self.resolver.resolve_dependents(task.id, commit=False)
            if task.project_id is not None:
                await self.resolver.reconcile_project(task.project_id, commit=False)
            await self.session.commit()
            return task
        except Exception:
            await self.session.rollback()
            raise

    async def request_fix(self, task_id: UUID, feedback: str) -> Task:
        try:
            task = await self._lock_reviewable(task_id)
            review = await self._record(task, TaskReviewDecision.FIX_REQUESTED, feedback.strip())
            task = await TaskStateMachine(self.session).transition(
                task.id,
                TaskStatus.FIX_REQUIRED,
                "Human review requested another execution iteration.",
                commit=False,
            )
            await self._review_event(task, review, "TASK_FIX_REQUESTED")
            task = await TaskStateMachine(self.session).transition(
                task.id,
                TaskStatus.QUEUED,
                "Task requeued with persisted human review feedback.",
                commit=False,
            )
            await self.session.commit()
            return task
        except Exception:
            await self.session.rollback()
            raise

    async def _lock_reviewable(self, task_id: UUID) -> Task:
        task = await self.tasks.get_for_update(task_id)
        if task is None:
            raise EntityNotFoundError("Task")
        if task.status != TaskStatus.REVIEW:
            raise TaskReviewConflictError("Task is no longer awaiting a review decision")
        if task.iteration < 1:
            raise TaskReviewConflictError("Task has no completed execution iteration to review")
        if await self.reviews.get_for_iteration(task.id, task.iteration) is not None:
            raise TaskReviewConflictError()
        return task

    async def _record(
        self,
        task: Task,
        decision: TaskReviewDecision,
        feedback: str | None,
    ) -> TaskReview:
        task_run = await self.session.scalar(
            select(TaskRun).where(
                TaskRun.task_id == task.id,
                TaskRun.iteration == task.iteration,
            )
        )
        agent_run = None
        if task_run is not None:
            agent_run = await self.session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.task_run_id == task_run.id,
                    AgentRun.status == AgentRunStatus.SUCCEEDED,
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            )
        return await self.reviews.add(
            TaskReview(
                task_id=task.id,
                task_run_id=task_run.id if task_run else None,
                agent_run_id=agent_run.id if agent_run else None,
                iteration=task.iteration,
                decision=decision,
                feedback=feedback,
                correlation_id=task.id,
            )
        )

    async def _review_event(self, task: Task, review: TaskReview, event_type: str) -> None:
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=review.correlation_id,
            event_type=event_type,
            message=(
                "Human reviewer approved the task result."
                if review.decision == TaskReviewDecision.APPROVED
                else "Human reviewer requested task changes."
            ),
            payload={
                "review_id": str(review.id),
                "task_id": str(task.id),
                "iteration": review.iteration,
                "decision": review.decision.value,
            },
        )
