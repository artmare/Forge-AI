from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskRunStatus, TaskStatus
from app.domain.exceptions import (
    EntityNotFoundError,
    InvalidTaskTransitionError,
    TaskRunStateConflictError,
    TaskUnassignedError,
)
from app.domain.models import Task, TaskRun
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.services.event_factory import EventFactory


class TaskStateMachine:
    ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
        TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
        TaskStatus.QUEUED: frozenset(
            {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED, TaskStatus.FAILED}
        ),
        TaskStatus.IN_PROGRESS: frozenset(
            {
                TaskStatus.REVIEW,
                TaskStatus.FIX_REQUIRED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.REVIEW: frozenset(
            {
                TaskStatus.DONE,
                TaskStatus.FIX_REQUIRED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.FIX_REQUIRED: frozenset(
            {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.DONE: frozenset(),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }
    TERMINAL_STATES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tasks = TaskRepository(session)
        self.runs = TaskRunRepository(session)
        self.events = EventFactory(session)

    @classmethod
    def can_transition(cls, current: TaskStatus, target: TaskStatus) -> bool:
        return target in cls.ALLOWED_TRANSITIONS[current]

    async def transition(
        self,
        task_id: UUID,
        target: TaskStatus,
        reason: str | None = None,
        *,
        commit: bool = True,
    ) -> Task:
        try:
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                raise EntityNotFoundError("Task")

            current = task.status
            if not self.can_transition(current, target):
                raise InvalidTaskTransitionError(current.value, target.value)

            normalized_reason = reason.strip() if reason else None
            now = datetime.now(UTC)

            if current == TaskStatus.QUEUED and target == TaskStatus.IN_PROGRESS:
                if task.assigned_agent_id is None:
                    raise TaskUnassignedError()
                if task.iteration >= task.max_iterations:
                    await self._fail_for_max_iterations(task, normalized_reason, now)
                    if commit:
                        await self.session.commit()
                    return task
                await self._start_execution(task, now)

            if current == TaskStatus.IN_PROGRESS:
                if target == TaskStatus.REVIEW:
                    await self._close_active_run(task, TaskRunStatus.SUCCEEDED, now)
                elif target == TaskStatus.FAILED:
                    await self._close_active_run(
                        task, TaskRunStatus.FAILED, now, error=normalized_reason
                    )
                elif target == TaskStatus.FIX_REQUIRED:
                    await self._close_active_run(
                        task, TaskRunStatus.FAILED, now, error=normalized_reason
                    )
                elif target == TaskStatus.CANCELLED:
                    await self._close_active_run(task, TaskRunStatus.CANCELLED, now)

            task.status = target
            task.completed_at = now if target in self.TERMINAL_STATES else None
            await self._record_transition(task, current, target, normalized_reason)

            if target == TaskStatus.CANCELLED:
                await self._add_event(
                    task,
                    "TASK_CANCELLED",
                    normalized_reason or "Task was cancelled.",
                    {"reason": normalized_reason, "iteration": task.iteration},
                )

            if commit:
                await self.session.commit()
            return task
        except Exception:
            await self.session.rollback()
            raise

    async def list_runs(self, task_id: UUID, offset: int = 0, limit: int = 100) -> list[TaskRun]:
        if await self.tasks.get(task_id) is None:
            raise EntityNotFoundError("Task")
        return await self.runs.list_for_task(task_id, offset, limit)

    async def recover_orphaned_in_progress(
        self, task_id: UUID, reason: str, *, commit: bool = True
    ) -> Task:
        """Fail an IN_PROGRESS task only when its required active TaskRun is missing."""
        try:
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                raise EntityNotFoundError("Task")
            if task.status != TaskStatus.IN_PROGRESS:
                return task
            if await self.runs.get_active_for_task(task.id) is not None:
                raise TaskRunStateConflictError(
                    "Orphan recovery cannot fail a task with an active execution run"
                )
            now = datetime.now(UTC)
            current = task.status
            task.status = TaskStatus.FAILED
            task.completed_at = now
            await self._record_transition(task, current, TaskStatus.FAILED, reason)
            await self._add_event(
                task,
                "TASK_EXECUTION_RECOVERED",
                "Orphaned in-progress task was recovered as failed.",
                {"reason": reason, "recovered": True},
            )
            if commit:
                await self.session.commit()
            return task
        except Exception:
            await self.session.rollback()
            raise

    async def _start_execution(self, task: Task, now: datetime) -> None:
        if await self.runs.get_active_for_task(task.id) is not None:
            raise TaskRunStateConflictError("Task already has an active execution run")

        task.iteration += 1
        if task.started_at is None:
            task.started_at = now

        run = await self.runs.add(
            TaskRun(
                task_id=task.id,
                agent_id=task.assigned_agent_id,
                iteration=task.iteration,
                status=TaskRunStatus.STARTED,
                input=task.input,
                started_at=now,
            )
        )
        await self._add_event(
            task,
            "TASK_RUN_STARTED",
            f"Task execution iteration {task.iteration} started.",
            {"task_run_id": str(run.id), "iteration": task.iteration},
        )

    async def _close_active_run(
        self,
        task: Task,
        status: TaskRunStatus,
        now: datetime,
        *,
        error: str | None = None,
    ) -> None:
        run = await self.runs.get_active_for_task(task.id)
        if run is None:
            raise TaskRunStateConflictError("In-progress task has no active execution run")

        run.status = status
        run.completed_at = now
        if error is not None:
            run.error = {"reason": error}

        event_type = {
            TaskRunStatus.SUCCEEDED: "TASK_RUN_SUCCEEDED",
            TaskRunStatus.FAILED: "TASK_RUN_FAILED",
        }.get(status)
        if event_type is not None:
            await self._add_event(
                task,
                event_type,
                f"Task execution iteration {run.iteration} {status.value.lower()}.",
                {"task_run_id": str(run.id), "iteration": run.iteration},
            )

    async def _fail_for_max_iterations(
        self, task: Task, requested_reason: str | None, now: datetime
    ) -> None:
        current = task.status
        task.status = TaskStatus.FAILED
        task.completed_at = now
        reason = f"Maximum execution iterations reached ({task.iteration}/{task.max_iterations})."
        await self._record_transition(task, current, TaskStatus.FAILED, reason)
        await self._add_event(
            task,
            "TASK_MAX_ITERATIONS_EXCEEDED",
            reason,
            {
                "iteration": task.iteration,
                "max_iterations": task.max_iterations,
                "requested_target": TaskStatus.IN_PROGRESS.value,
                "requested_reason": requested_reason,
            },
        )

    async def _record_transition(
        self,
        task: Task,
        current: TaskStatus,
        target: TaskStatus,
        reason: str | None,
    ) -> None:
        await self._add_event(
            task,
            "TASK_STATUS_CHANGED",
            f"Task status changed from {current.value} to {target.value}.",
            {
                "from_status": current.value,
                "to_status": target.value,
                "reason": reason,
                "iteration": task.iteration,
            },
        )

    async def _add_event(
        self, task: Task, event_type: str, message: str, details: dict[str, object]
    ) -> None:
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type=event_type,
            message=message,
            payload=details,
        )
