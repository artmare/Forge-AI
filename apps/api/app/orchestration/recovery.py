import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.profile import DevelopmentProfileService
from app.development.service import DevelopmentExecutionService
from app.domain.enums import (
    AgentRunStatus,
    AgentStatus,
    ExecutionJobStatus,
    TaskRunStatus,
    TaskStatus,
    ToolCallStatus,
    WorkerStatus,
)
from app.domain.models import (
    Agent,
    AgentRun,
    ExecutionJob,
    ProjectDevelopmentLease,
    Task,
    TaskRun,
    ToolCall,
)
from app.repositories.execution_job import ExecutionJobRepository
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.repositories.worker_node import WorkerNodeRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


class OrchestrationRecoveryService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.jobs = ExecutionJobRepository(session)
        self.workers = WorkerNodeRepository(session)
        self.tasks = TaskRepository(session)
        self.task_runs = TaskRunRepository(session)
        self.events = EventFactory(session)

    async def recover(self, limit: int = 100) -> dict[str, int]:
        now = datetime.now(UTC)
        stale_development_executions = await DevelopmentExecutionService(
            self.session, settings=self.settings
        ).recover_stale(limit)
        refreshed_development_profiles = await DevelopmentProfileService(
            self.session, self.settings
        ).reconcile_active(limit)
        expired_project_leases = await self._recover_project_leases(now, limit)
        stale_workers = await self._recover_stale_workers(now, limit)
        expired_jobs = await self._recover_expired_jobs(now, limit)
        orphaned_agents = await self._recover_orphaned_agents(limit)
        orphaned_tasks = await self._recover_orphaned_tasks(limit)
        orphaned_runs = await self._recover_orphaned_task_runs(now, limit)
        await self.session.commit()
        result = {
            "stale_workers": stale_workers,
            "expired_jobs": expired_jobs,
            "orphaned_agents": orphaned_agents,
            "orphaned_tasks": orphaned_tasks,
            "orphaned_task_runs": orphaned_runs,
            "stale_development_executions": stale_development_executions,
            "refreshed_development_profiles": refreshed_development_profiles,
            "expired_project_leases": expired_project_leases,
        }
        if any(result.values()):
            logger.warning(
                "Orchestration recovery repaired stale state",
                extra={"event": "orchestration_recovery_completed", **result},
            )
        return result

    async def _recover_project_leases(self, now: datetime, limit: int) -> int:
        leases = list(
            await self.session.scalars(
                select(ProjectDevelopmentLease)
                .where(
                    ProjectDevelopmentLease.lease_owner.is_not(None),
                    ProjectDevelopmentLease.lease_expires_at < now,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for lease in leases:
            lease.lease_owner = None
            lease.lease_expires_at = None
            lease.updated_at = now
        return len(leases)

    async def _recover_stale_workers(self, now: datetime, limit: int) -> int:
        cutoff = now - timedelta(seconds=self.settings.worker_stale_seconds)
        workers = await self.workers.claim_stale(cutoff, limit)
        for worker in workers:
            worker.status = WorkerStatus.OFFLINE
            worker.stopped_at = now
            await self.events.create(
                event_type="WORKER_OFFLINE",
                message="Stale agent worker was marked offline.",
                payload={
                    "worker_id": str(worker.id),
                    "worker_key": worker.worker_key,
                    "recovered": True,
                },
            )
        return len(workers)

    async def _recover_expired_jobs(self, now: datetime, limit: int) -> int:
        jobs = await self.jobs.claim_expired(now, limit)
        for job in jobs:
            task = await self.tasks.get_for_update(job.task_id)
            task_run = await self.task_runs.get(job.task_run_id) if job.task_run_id else None
            await self.events.create(
                company_id=task.company_id if task else None,
                project_id=task.project_id if task else None,
                agent_id=job.agent_id,
                task_id=job.task_id,
                correlation_id=job.correlation_id,
                event_type="EXECUTION_JOB_LEASE_EXPIRED",
                message="Execution job lease expired and entered recovery.",
                payload={
                    "execution_job_id": str(job.id),
                    "worker_id": str(job.worker_id) if job.worker_id else None,
                    "attempts": job.attempts,
                    "previous_status": job.status.value,
                },
            )
            if task is None:
                await self._terminal_job(job, ExecutionJobStatus.FAILED, now, "TASK_NOT_ELIGIBLE")
                continue
            if task.status == TaskStatus.CANCELLED:
                await self._terminal_job(
                    job, ExecutionJobStatus.CANCELLED, now, "TASK_NOT_ELIGIBLE"
                )
                await self._release_agent(job.agent_id)
                continue
            if (
                task.status == TaskStatus.REVIEW
                and task_run is not None
                and task_run.status == TaskRunStatus.SUCCEEDED
            ):
                await self._terminal_job(job, ExecutionJobStatus.SUCCEEDED, now, None)
                await self._release_agent(job.agent_id)
                continue

            active_run = await self.task_runs.get_active_for_task(task.id)
            execution_started = task.status == TaskStatus.IN_PROGRESS or active_run is not None
            if (
                not execution_started
                and task.status == TaskStatus.QUEUED
                and job.attempts < job.max_attempts
            ):
                delay = self.settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0))
                job.status = ExecutionJobStatus.PENDING
                job.available_at = now + timedelta(seconds=delay)
                job.worker_id = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = {
                    "code": "LEASE_EXPIRED",
                    "message": "Worker lease expired before durable task execution began.",
                }
                job.retry_history = [
                    *job.retry_history,
                    {
                        "attempt": job.attempts,
                        "at": now.isoformat(),
                        "code": "LEASE_EXPIRED",
                        "message": "Worker lease expired before durable task execution began.",
                        "outcome": "RETRY_SCHEDULED",
                    },
                ][-20:]
                await self._release_agent(job.agent_id)
                continue

            error_code = (
                "EXECUTION_RETRY_EXHAUSTED"
                if not execution_started and job.attempts >= job.max_attempts
                else "WORKER_LOST"
            )
            message = (
                "Execution infrastructure attempts were exhausted."
                if error_code == "EXECUTION_RETRY_EXHAUSTED"
                else "Worker was lost during execution; side-effect state may be ambiguous."
            )
            await self._fail_active_runtime_state(task, active_run, message, now)
            if task.status == TaskStatus.IN_PROGRESS and active_run is None:
                await TaskStateMachine(self.session).recover_orphaned_in_progress(
                    task.id, message, commit=False
                )
            elif task.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
                await TaskStateMachine(self.session).transition(
                    task.id, TaskStatus.FAILED, message, commit=False
                )
            await self._terminal_job(job, ExecutionJobStatus.FAILED, now, error_code, message)
            await self._release_agent(job.agent_id)
        return len(jobs)

    async def _fail_active_runtime_state(
        self, task: Task, task_run: TaskRun | None, message: str, now: datetime
    ) -> None:
        if task_run is None:
            task_run = await self.task_runs.get_active_for_task(task.id)
        if task_run is None:
            return
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_run_id == task_run.id,
                    ToolCall.status.in_(
                        (
                            ToolCallStatus.REQUESTED,
                            ToolCallStatus.AUTHORIZED,
                            ToolCallStatus.RUNNING,
                        )
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for call in calls:
            call.status = ToolCallStatus.FAILED
            call.completed_at = now
            call.error = {"code": "WORKER_LOST", "message": message}
        runs = list(
            await self.session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.task_run_id == task_run.id,
                    AgentRun.status.in_((AgentRunStatus.CREATED, AgentRunStatus.RUNNING)),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for run in runs:
            run.status = AgentRunStatus.FAILED
            run.completed_at = now
            run.error_code = "WORKER_LOST"
            run.error_message = message

    async def _terminal_job(
        self,
        job: ExecutionJob,
        status: ExecutionJobStatus,
        now: datetime,
        error_code: str | None,
        message: str | None = None,
    ) -> None:
        job.status = status
        job.completed_at = now
        if error_code is not None:
            normalized_message = message or "Execution job recovery failed."
            job.last_error = {
                "code": error_code,
                "message": normalized_message,
            }
            job.failure_evidence = {
                "category": "INFRASTRUCTURE_FAILURE",
                "code": error_code,
                "message": normalized_message,
                "phase": job.phase.value,
                "failed_at": now.isoformat(),
                "execution_job_id": str(job.id),
                "task_id": str(job.task_id),
                "task_run_id": str(job.task_run_id) if job.task_run_id else None,
                "agent_id": str(job.agent_id),
                "worker_id": str(job.worker_id) if job.worker_id else None,
                "attempt": job.attempts,
                "environment": job.environment_state,
                "checkpoint": job.checkpoint_state,
                "working_tree": job.working_tree_state,
            }
            if error_code == "EXECUTION_RETRY_EXHAUSTED":
                job.retry_history = [
                    *job.retry_history,
                    {
                        "attempt": job.attempts,
                        "at": now.isoformat(),
                        "code": error_code,
                        "message": normalized_message,
                        "outcome": "RETRY_EXHAUSTED",
                    },
                ][-20:]

    async def _release_agent(self, agent_id) -> None:  # type: ignore[no-untyped-def]
        agent = await self.session.scalar(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
        if agent is not None and agent.status == AgentStatus.BUSY:
            agent.status = AgentStatus.IDLE
            await self.events.create(
                company_id=agent.company_id,
                agent_id=agent.id,
                event_type="AGENT_STATUS_CHANGED",
                message="Recovery changed agent status from BUSY to IDLE.",
                payload={
                    "from": AgentStatus.BUSY.value,
                    "to": AgentStatus.IDLE.value,
                    "recovered": True,
                },
            )

    async def _recover_orphaned_agents(self, limit: int) -> int:
        agents = list(
            await self.session.scalars(
                select(Agent)
                .where(
                    Agent.status == AgentStatus.BUSY,
                    ~select(ExecutionJob.id)
                    .where(
                        ExecutionJob.agent_id == Agent.id,
                        ExecutionJob.status.in_(
                            (ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)
                        ),
                    )
                    .exists(),
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for agent in agents:
            agent.status = AgentStatus.IDLE
        return len(agents)

    async def _recover_orphaned_tasks(self, limit: int) -> int:
        tasks = list(
            await self.session.scalars(
                select(Task)
                .where(
                    Task.status == TaskStatus.IN_PROGRESS,
                    ~select(TaskRun.id)
                    .where(
                        TaskRun.task_id == Task.id,
                        TaskRun.status == TaskRunStatus.STARTED,
                    )
                    .exists(),
                    ~select(ExecutionJob.id)
                    .where(
                        ExecutionJob.task_id == Task.id,
                        ExecutionJob.status.in_(ExecutionJobRepository.ACTIVE_STATUSES),
                    )
                    .exists(),
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for task in tasks:
            await TaskStateMachine(self.session).recover_orphaned_in_progress(
                task.id,
                "In-progress task had no active TaskRun or execution job.",
                commit=False,
            )
        return len(tasks)

    async def _recover_orphaned_task_runs(self, now: datetime, limit: int) -> int:
        cutoff = now - timedelta(seconds=self.settings.agent_run_stale_seconds)
        rows = list(
            await self.session.scalars(
                select(TaskRun)
                .join(Task, Task.id == TaskRun.task_id)
                .where(
                    TaskRun.status == TaskRunStatus.STARTED,
                    TaskRun.started_at < cutoff,
                    Task.status == TaskStatus.IN_PROGRESS,
                    ~select(ExecutionJob.id)
                    .where(
                        ExecutionJob.task_id == Task.id,
                        ExecutionJob.status.in_(ExecutionJobRepository.ACTIVE_STATUSES),
                    )
                    .exists(),
                    ~select(AgentRun.id)
                    .where(
                        AgentRun.task_run_id == TaskRun.id,
                        AgentRun.status.in_((AgentRunStatus.CREATED, AgentRunStatus.RUNNING)),
                    )
                    .exists(),
                )
                .limit(limit)
                .with_for_update(of=TaskRun, skip_locked=True)
            )
        )
        for run in rows:
            task = await self.tasks.get_for_update(run.task_id)
            if task is not None and task.status == TaskStatus.IN_PROGRESS:
                await TaskStateMachine(self.session).transition(
                    task.id,
                    TaskStatus.FAILED,
                    "In-progress task had no active execution job.",
                    commit=False,
                )
        return len(rows)
