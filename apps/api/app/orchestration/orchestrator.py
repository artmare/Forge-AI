import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import (
    AgentStatus,
    ExecutionJobStatus,
    TaskPriority,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.models import Agent, ExecutionJob, Task, TaskRun
from app.repositories.execution_job import ExecutionJobRepository
from app.repositories.runtime_control import RuntimeControlRepository
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.services.event_factory import EventFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    eligible_tasks: int
    jobs_created: int
    jobs_skipped: int
    jobs_cancelled: int
    duration_ms: float
    paused: bool


class OrchestratorService:
    NON_EXECUTABLE_AGENT_STATES = (
        AgentStatus.PAUSED,
        AgentStatus.STOPPED,
        AgentStatus.FAILED,
    )

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = RuntimeControlRepository(session)
        self.jobs = ExecutionJobRepository(session)
        self.tasks = TaskRepository(session)
        self.task_runs = TaskRunRepository(session)
        self.events = EventFactory(session)

    async def schedule_task(
        self,
        task_id: UUID,
        *,
        causation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ExecutionJob | None:
        try:
            control = await self.controls.get_for_update()
            if control is None:
                control = await self.controls.ensure(self.settings.autonomy_enabled)
            if not control.enabled:
                await self.session.rollback()
                return None
            task = await self.tasks.get_for_update(task_id)
            if task is None or task.status != TaskStatus.QUEUED:
                await self.session.rollback()
                return None
            if task.assigned_agent_id is None or task.iteration >= task.max_iterations:
                await self.session.rollback()
                return None
            agent = await self.session.get(Agent, task.assigned_agent_id)
            if (
                agent is None
                or agent.company_id != task.company_id
                or agent.status in self.NON_EXECUTABLE_AGENT_STATES
            ):
                await self.session.rollback()
                return None
            if await self.jobs.get_active_for_task(task.id) is not None:
                await self.session.rollback()
                return None
            if await self.task_runs.get_active_for_task(task.id) is not None:
                await self.session.rollback()
                return None
            job = await self.jobs.add(
                ExecutionJob(
                    task_id=task.id,
                    agent_id=agent.id,
                    status=ExecutionJobStatus.PENDING,
                    priority=task.priority,
                    attempts=0,
                    max_attempts=self.settings.execution_job_max_attempts,
                    available_at=datetime.now(UTC),
                    correlation_id=correlation_id or task.id,
                )
            )
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                agent_id=agent.id,
                task_id=task.id,
                correlation_id=job.correlation_id,
                causation_id=causation_id,
                event_type="EXECUTION_JOB_CREATED",
                message="Execution job created for queued task.",
                payload={
                    "execution_job_id": str(job.id),
                    "status": job.status.value,
                    "priority": job.priority.value,
                    "max_attempts": job.max_attempts,
                },
            )
            await self.session.commit()
            return job
        except IntegrityError:
            await self.session.rollback()
            return None
        except Exception:
            await self.session.rollback()
            raise

    async def reconcile(self, limit: int = 500) -> ReconciliationResult:
        started = perf_counter()
        control = await self.controls.ensure(self.settings.autonomy_enabled)
        if not control.enabled:
            control.last_reconciled_at = datetime.now(UTC)
            await self.session.commit()
            return ReconciliationResult(0, 0, 0, 0, (perf_counter() - started) * 1000, True)

        cancelled = await self._cancel_stale_pending()
        priority_order = case(
            (Task.priority == TaskPriority.CRITICAL, 0),
            (Task.priority == TaskPriority.HIGH, 1),
            (Task.priority == TaskPriority.NORMAL, 2),
            else_=3,
        )
        task_ids = list(
            await self.session.scalars(
                select(Task.id)
                .join(Agent, Agent.id == Task.assigned_agent_id)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    Task.iteration < Task.max_iterations,
                    Agent.company_id == Task.company_id,
                    Agent.status.not_in(self.NON_EXECUTABLE_AGENT_STATES),
                    ~select(ExecutionJob.id)
                    .where(
                        ExecutionJob.task_id == Task.id,
                        ExecutionJob.status.in_(ExecutionJobRepository.ACTIVE_STATUSES),
                    )
                    .exists(),
                    ~select(TaskRun.id)
                    .where(
                        TaskRun.task_id == Task.id,
                        TaskRun.status == TaskRunStatus.STARTED,
                    )
                    .exists(),
                )
                .order_by(priority_order, Task.created_at, Task.id)
                .limit(limit)
            )
        )
        await self.session.commit()
        created = 0
        for task_id in task_ids:
            if await self.schedule_task(task_id) is not None:
                created += 1

        control = await self.controls.get_for_update()
        if control is not None:
            control.last_reconciled_at = datetime.now(UTC)
        await self.session.commit()
        result = ReconciliationResult(
            eligible_tasks=len(task_ids),
            jobs_created=created,
            jobs_skipped=len(task_ids) - created,
            jobs_cancelled=cancelled,
            duration_ms=(perf_counter() - started) * 1000,
            paused=False,
        )
        logger.info(
            "Orchestrator reconciliation completed",
            extra={
                "event": "orchestrator_reconciled",
                "eligible_task_count": result.eligible_tasks,
                "jobs_created": result.jobs_created,
                "jobs_skipped": result.jobs_skipped,
                "jobs_cancelled": result.jobs_cancelled,
                "reconciliation_duration_ms": round(result.duration_ms, 2),
                "paused": result.paused,
            },
        )
        return result

    async def _cancel_stale_pending(self) -> int:
        jobs = list(
            await self.session.scalars(
                select(ExecutionJob)
                .join(Task, Task.id == ExecutionJob.task_id)
                .where(
                    ExecutionJob.status == ExecutionJobStatus.PENDING,
                    Task.status != TaskStatus.QUEUED,
                )
                .with_for_update(of=ExecutionJob, skip_locked=True)
            )
        )
        now = datetime.now(UTC)
        for job in jobs:
            job.status = ExecutionJobStatus.CANCELLED
            job.completed_at = now
            job.last_error = {
                "code": "TASK_NOT_ELIGIBLE",
                "message": "Task is no longer queued.",
            }
        await self.session.commit()
        return len(jobs)
