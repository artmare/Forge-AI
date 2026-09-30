from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import (
    AgentStatus,
    ExecutionJobStatus,
    ExecutionPhase,
    TaskKind,
    TaskPriority,
    TaskStatus,
    WorkerStatus,
)
from app.domain.models import (
    Agent,
    AgentRun,
    DevelopmentExecution,
    Event,
    ExecutionJob,
    ProjectDevelopmentLease,
    ProjectDevelopmentProfile,
    Task,
    TaskRun,
    TaskRuntimeBudget,
    TaskRuntimeMetric,
    ToolCall,
    WorkerNode,
)
from app.repositories.execution_job import ExecutionJobRepository
from app.repositories.runtime_control import RuntimeControlRepository
from app.repositories.task import TaskRepository
from app.repositories.worker_node import WorkerNodeRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine


class WorkerExecutionService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = RuntimeControlRepository(session)
        self.jobs = ExecutionJobRepository(session)
        self.workers = WorkerNodeRepository(session)
        self.tasks = TaskRepository(session)
        self.events = EventFactory(session)

    async def register(self, worker_key: str, concurrency: int) -> WorkerNode:
        now = datetime.now(UTC)
        worker = await self.workers.add(
            WorkerNode(
                worker_key=worker_key,
                status=WorkerStatus.ONLINE,
                concurrency=concurrency,
                started_at=now,
                last_heartbeat_at=now,
                metadata_={"runtime": "forge-agent-worker"},
            )
        )
        await self.events.create(
            event_type="WORKER_ONLINE",
            message="Agent worker registered online.",
            payload={
                "worker_id": str(worker.id),
                "worker_key": worker.worker_key,
                "concurrency": worker.concurrency,
            },
        )
        await self.session.commit()
        return worker

    async def heartbeat(self, worker_id: UUID) -> bool:
        worker = await self.workers.get_for_update(worker_id)
        if worker is None or worker.status == WorkerStatus.OFFLINE:
            await self.session.rollback()
            return False
        worker.last_heartbeat_at = datetime.now(UTC)
        await self.session.commit()
        return True

    async def set_status(self, worker_id: UUID, status: WorkerStatus) -> None:
        worker = await self.workers.get_for_update(worker_id)
        if worker is None:
            await self.session.rollback()
            return
        old_status = worker.status
        worker.status = status
        if status == WorkerStatus.OFFLINE:
            worker.stopped_at = datetime.now(UTC)
        if old_status != status and status == WorkerStatus.OFFLINE:
            await self.events.create(
                event_type="WORKER_OFFLINE",
                message="Agent worker went offline.",
                payload={"worker_id": str(worker.id), "worker_key": worker.worker_key},
            )
        await self.session.commit()

    async def claim(self, worker_id: UUID, lease_owner: str) -> ExecutionJob | None:
        try:
            control = await self.controls.get_for_update()
            if control is None:
                control = await self.controls.ensure(self.settings.autonomy_enabled)
            if not control.enabled:
                await self.session.rollback()
                return None
            worker = await self.workers.get_for_update(worker_id)
            if worker is None or worker.status != WorkerStatus.ONLINE:
                await self.session.rollback()
                return None
            if await self.jobs.count_executing() >= self.settings.forge_max_concurrent_tasks:
                await self.session.rollback()
                return None

            priority_order = case(
                (ExecutionJob.priority == TaskPriority.CRITICAL, 0),
                (ExecutionJob.priority == TaskPriority.HIGH, 1),
                (ExecutionJob.priority == TaskPriority.NORMAL, 2),
                else_=3,
            )
            other_job = ExecutionJob.__table__.alias("other_active_agent_job")
            now = datetime.now(UTC)
            job = await self.session.scalar(
                select(ExecutionJob)
                .join(Task, Task.id == ExecutionJob.task_id)
                .join(Agent, Agent.id == ExecutionJob.agent_id)
                .where(
                    ExecutionJob.status == ExecutionJobStatus.PENDING,
                    ExecutionJob.available_at <= now,
                    ExecutionJob.attempts < ExecutionJob.max_attempts,
                    Task.status == TaskStatus.QUEUED,
                    Task.assigned_agent_id == ExecutionJob.agent_id,
                    Task.iteration < Task.max_iterations,
                    Agent.company_id == Task.company_id,
                    Agent.status.in_((AgentStatus.CREATED, AgentStatus.IDLE)),
                    ~select(other_job.c.id)
                    .where(
                        other_job.c.agent_id == ExecutionJob.agent_id,
                        other_job.c.status.in_(("CLAIMED", "RUNNING")),
                    )
                    .exists(),
                )
                .order_by(priority_order, ExecutionJob.available_at, ExecutionJob.created_at)
                .limit(1)
                .with_for_update(of=ExecutionJob, skip_locked=True)
            )
            if job is None:
                await self.session.rollback()
                return None
            job.status = ExecutionJobStatus.CLAIMED
            job.attempts += 1
            job.worker_id = worker.id
            job.lease_owner = lease_owner
            job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            job.last_error = None
            task = await self.tasks.get(job.task_id)
            await self.events.create(
                company_id=task.company_id if task else None,
                project_id=task.project_id if task else None,
                agent_id=job.agent_id,
                task_id=job.task_id,
                correlation_id=job.correlation_id,
                event_type="EXECUTION_JOB_CLAIMED",
                message="Execution job claimed by an agent worker.",
                payload={
                    "execution_job_id": str(job.id),
                    "worker_id": str(worker.id),
                    "attempt": job.attempts,
                    "lease_expires_at": job.lease_expires_at.isoformat(),
                },
            )
            await self.session.commit()
            return job
        except Exception:
            await self.session.rollback()
            raise

    async def start(self, job_id: UUID, lease_owner: str) -> ExecutionJob | None:
        try:
            control = await self.controls.get_for_update()
            if control is None or not control.enabled:
                job = await self.jobs.get_for_update(job_id)
                if job is not None and job.status == ExecutionJobStatus.CLAIMED:
                    job.status = ExecutionJobStatus.PENDING
                    job.attempts = max(job.attempts - 1, 0)
                    job.worker_id = None
                    job.lease_owner = None
                    job.lease_expires_at = None
                await self.session.commit()
                return None
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.status != ExecutionJobStatus.CLAIMED
                or job.lease_owner != lease_owner
            ):
                await self.session.rollback()
                return None
            task = await self.tasks.get_for_update(job.task_id)
            agent = await self.session.scalar(
                select(Agent).where(Agent.id == job.agent_id).with_for_update()
            )
            if task is None or task.status != TaskStatus.QUEUED:
                await self._cancel_locked_job(job, "TASK_NOT_ELIGIBLE", "Task is no longer queued.")
                await self.session.commit()
                return None
            if task.kind in (TaskKind.DEVELOPMENT, TaskKind.QA) and task.project_id is not None:
                if not await self._acquire_project_lease(
                    task.project_id, f"{lease_owner}:{job.id}"
                ):
                    job.status = ExecutionJobStatus.PENDING
                    job.attempts = max(job.attempts - 1, 0)
                    job.worker_id = None
                    job.lease_owner = None
                    job.lease_expires_at = None
                    await self.session.commit()
                    return None
            if (
                agent is None
                or agent.company_id != task.company_id
                or agent.status not in (AgentStatus.CREATED, AgentStatus.IDLE)
            ):
                await self._fail_locked_job(
                    job, "AGENT_NOT_EXECUTABLE", "Assigned agent is not executable."
                )
                await self.session.commit()
                return None
            old_status = agent.status
            agent.status = AgentStatus.BUSY
            job.status = ExecutionJobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            job.phase = ExecutionPhase.PREPARING
            job.phase_history = [
                *job.phase_history,
                self._phase_record(ExecutionPhase.PREPARING, job.attempts),
            ]
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                agent_id=agent.id,
                task_id=task.id,
                correlation_id=job.correlation_id,
                event_type="AGENT_STATUS_CHANGED",
                message=f"Agent status changed from {old_status.value} to BUSY.",
                payload={"from": old_status.value, "to": AgentStatus.BUSY.value},
            )
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                agent_id=agent.id,
                task_id=task.id,
                correlation_id=job.correlation_id,
                event_type="EXECUTION_JOB_STARTED",
                message="Execution job started through AgentRuntime.",
                payload={"execution_job_id": str(job.id), "worker_id": str(job.worker_id)},
            )
            await self.session.commit()
            return job
        except Exception:
            await self.session.rollback()
            raise

    async def update_phase(
        self,
        job_id: UUID,
        lease_owner: str,
        phase: ExecutionPhase,
        evidence: dict | None = None,
    ) -> bool:
        """Persist a short execution phase transition without holding runtime locks."""
        try:
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.status != ExecutionJobStatus.RUNNING
                or job.lease_owner != lease_owner
            ):
                await self.session.rollback()
                return False
            safe_evidence = self._bounded_evidence(evidence or {})
            history = list(job.phase_history)
            if history and job.phase == phase and history[-1].get("phase") == phase.value:
                current = dict(history[-1])
                current["evidence"] = {
                    **current.get("evidence", {}),
                    **safe_evidence,
                }
                history[-1] = current
            else:
                history.append(self._phase_record(phase, job.attempts, safe_evidence))
            job.phase = phase
            job.phase_history = history[-100:]
            if phase == ExecutionPhase.ENVIRONMENT_SETUP:
                job.environment_state = {**job.environment_state, **safe_evidence}
            elif phase == ExecutionPhase.CHECKPOINTING:
                job.checkpoint_state = {**job.checkpoint_state, **safe_evidence}
            tree = safe_evidence.get("working_tree")
            if isinstance(tree, dict):
                job.working_tree_state = {**job.working_tree_state, **tree}
            await self.session.commit()
            return True
        except Exception:
            await self.session.rollback()
            raise

    async def renew_lease(self, job_id: UUID, lease_owner: str) -> bool:
        try:
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.status not in (ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)
                or job.lease_owner != lease_owner
            ):
                await self.session.rollback()
                return False
            job.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.settings.job_lease_seconds
            )
            task = await self.tasks.get(job.task_id)
            if (
                task is not None
                and task.kind in (TaskKind.DEVELOPMENT, TaskKind.QA)
                and task.project_id is not None
            ):
                lease = await self.session.scalar(
                    select(ProjectDevelopmentLease)
                    .where(ProjectDevelopmentLease.project_id == task.project_id)
                    .with_for_update()
                )
                if lease is not None and lease.lease_owner == f"{lease_owner}:{job.id}":
                    lease.lease_expires_at = datetime.now(UTC) + timedelta(
                        seconds=self.settings.development_project_lease_seconds
                    )
            await self.session.commit()
            return True
        except Exception:
            await self.session.rollback()
            raise

    async def succeed(self, job_id: UUID, lease_owner: str, task_run_id: UUID) -> bool:
        try:
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.status != ExecutionJobStatus.RUNNING
                or job.lease_owner != lease_owner
            ):
                await self.session.rollback()
                return False
            task = await self.tasks.get_for_update(job.task_id)
            now = datetime.now(UTC)
            job.task_run_id = task_run_id
            job.completed_at = now
            if task is not None and task.status == TaskStatus.CANCELLED:
                job.status = ExecutionJobStatus.CANCELLED
                job.last_error = {
                    "code": "AGENT_EXECUTION_CANCELLED",
                    "message": "Task was cancelled before execution completion was committed.",
                }
            else:
                job.status = ExecutionJobStatus.SUCCEEDED
            agent = await self.session.scalar(
                select(Agent).where(Agent.id == job.agent_id).with_for_update()
            )
            agent_became_idle = False
            if agent is not None and agent.status == AgentStatus.BUSY:
                agent.status = AgentStatus.IDLE
                agent_became_idle = True
            await self._release_project_lease(task, f"{lease_owner}:{job.id}")
            if agent_became_idle:
                await self.events.create(
                    company_id=task.company_id if task else None,
                    project_id=task.project_id if task else None,
                    agent_id=job.agent_id,
                    task_id=job.task_id,
                    correlation_id=job.correlation_id,
                    event_type="AGENT_STATUS_CHANGED",
                    message="Agent status changed from BUSY to IDLE.",
                    payload={"from": AgentStatus.BUSY.value, "to": AgentStatus.IDLE.value},
                )
            await self.events.create(
                company_id=task.company_id if task else None,
                project_id=task.project_id if task else None,
                agent_id=job.agent_id,
                task_id=job.task_id,
                correlation_id=job.correlation_id,
                event_type=(
                    "EXECUTION_JOB_SUCCEEDED"
                    if job.status == ExecutionJobStatus.SUCCEEDED
                    else "EXECUTION_JOB_FAILED"
                ),
                message=(
                    "Execution job completed successfully."
                    if job.status == ExecutionJobStatus.SUCCEEDED
                    else "Execution job was cancelled before completion was committed."
                ),
                payload={
                    "execution_job_id": str(job.id),
                    "task_run_id": str(task_run_id),
                    "attempts": job.attempts,
                },
            )
            await self.session.commit()
            return job.status == ExecutionJobStatus.SUCCEEDED
        except Exception:
            await self.session.rollback()
            raise

    async def retry_infrastructure(
        self, job_id: UUID, lease_owner: str, code: str, message: str
    ) -> bool:
        """Retry bootstrap/runtime infrastructure without consuming a Developer fix iteration."""
        try:
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.status not in (ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)
                or job.lease_owner != lease_owner
            ):
                await self.session.rollback()
                return False
            task = await self.tasks.get_for_update(job.task_id)
            now = datetime.now(UTC)
            retrying = job.attempts < job.max_attempts
            job.last_error = {"code": code, "message": message}
            failure = await self._normalized_failure(job, task, code, message)
            job.failure_evidence = failure
            job.retry_history = [
                *job.retry_history,
                {
                    "attempt": job.attempts,
                    "at": now.isoformat(),
                    "code": code,
                    "message": message[:1000],
                    "outcome": "RETRY_SCHEDULED" if retrying else "RETRY_EXHAUSTED",
                },
            ][-20:]
            await self._release_project_lease(task, f"{lease_owner}:{job.id}")
            agent = await self.session.scalar(
                select(Agent).where(Agent.id == job.agent_id).with_for_update()
            )
            if agent is not None and agent.status == AgentStatus.BUSY:
                agent.status = AgentStatus.IDLE
            if retrying:
                delay = self.settings.job_retry_base_seconds * (2 ** max(job.attempts - 1, 0))
                job.status = ExecutionJobStatus.PENDING
                job.available_at = now + timedelta(seconds=delay)
                job.worker_id = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.started_at = None
                await self.events.create(
                    company_id=task.company_id if task else None,
                    project_id=task.project_id if task else None,
                    agent_id=job.agent_id,
                    task_id=job.task_id,
                    correlation_id=job.correlation_id,
                    event_type="EXECUTION_JOB_RETRY_SCHEDULED",
                    message="Development infrastructure retry scheduled.",
                    payload={
                        "execution_job_id": str(job.id),
                        "error_code": code,
                        "attempts": job.attempts,
                        "max_attempts": job.max_attempts,
                        "available_at": job.available_at.isoformat(),
                    },
                )
            else:
                if task is not None and task.status == TaskStatus.QUEUED:
                    await TaskStateMachine(self.session).transition(
                        task.id,
                        TaskStatus.FAILED,
                        f"Development infrastructure retry exhausted: {message}",
                        commit=False,
                    )
                job.status = ExecutionJobStatus.FAILED
                job.completed_at = now
                await self.events.create(
                    company_id=task.company_id if task else None,
                    project_id=task.project_id if task else None,
                    agent_id=job.agent_id,
                    task_id=job.task_id,
                    correlation_id=job.correlation_id,
                    event_type="EXECUTION_JOB_FAILED",
                    message="Development infrastructure retries exhausted.",
                    payload={
                        "execution_job_id": str(job.id),
                        "error_code": code,
                        "attempts": job.attempts,
                    },
                )
            await self.session.commit()
            return retrying
        except Exception:
            await self.session.rollback()
            raise

    async def fail(
        self,
        job_id: UUID,
        lease_owner: str,
        code: str,
        message: str,
        task_run_id: UUID | None = None,
        internal_error: dict | None = None,
    ) -> bool:
        try:
            job = await self.jobs.get_for_update(job_id)
            if (
                job is None
                or job.lease_owner != lease_owner
                or job.status
                not in (
                    ExecutionJobStatus.CLAIMED,
                    ExecutionJobStatus.RUNNING,
                )
            ):
                await self.session.rollback()
                return False
            task = await self.tasks.get_for_update(job.task_id)
            if task is not None and task.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
                await TaskStateMachine(self.session).transition(
                    task.id, TaskStatus.FAILED, message, commit=False
                )
            now = datetime.now(UTC)
            job.status = (
                ExecutionJobStatus.CANCELLED
                if task is not None and task.status == TaskStatus.CANCELLED
                else ExecutionJobStatus.FAILED
            )
            job.task_run_id = task_run_id
            job.completed_at = now
            job.last_error = {"code": code, "message": message}
            job.failure_evidence = await self._normalized_failure(
                job, task, code, message, task_run_id
            )
            if internal_error:
                job.internal_error = {
                    str(key): str(value)[:16_000]
                    for key, value in internal_error.items()
                    if key in {"exception_type", "message", "stack"}
                }
            await self._release_project_lease(task, f"{lease_owner}:{job.id}")
            agent = await self.session.scalar(
                select(Agent).where(Agent.id == job.agent_id).with_for_update()
            )
            agent_became_idle = False
            if agent is not None and agent.status == AgentStatus.BUSY:
                agent.status = AgentStatus.IDLE
                agent_became_idle = True
            if agent_became_idle:
                await self.events.create(
                    company_id=task.company_id if task else None,
                    project_id=task.project_id if task else None,
                    agent_id=job.agent_id,
                    task_id=job.task_id,
                    correlation_id=job.correlation_id,
                    event_type="AGENT_STATUS_CHANGED",
                    message="Agent status changed from BUSY to IDLE.",
                    payload={"from": AgentStatus.BUSY.value, "to": AgentStatus.IDLE.value},
                )
            await self.events.create(
                company_id=task.company_id if task else None,
                project_id=task.project_id if task else None,
                agent_id=job.agent_id,
                task_id=job.task_id,
                correlation_id=job.correlation_id,
                event_type="EXECUTION_JOB_FAILED",
                message="Execution job failed.",
                payload={
                    "execution_job_id": str(job.id),
                    "task_run_id": str(task_run_id) if task_run_id else None,
                    "error_code": code,
                    "attempts": job.attempts,
                },
            )
            await self.session.commit()
            return True
        except Exception:
            await self.session.rollback()
            raise

    async def _acquire_project_lease(self, project_id: UUID, owner: str) -> bool:
        now = datetime.now(UTC)
        await self.session.execute(
            insert(ProjectDevelopmentLease)
            .values(project_id=project_id)
            .on_conflict_do_nothing(index_elements=["project_id"])
        )
        lease = await self.session.scalar(
            select(ProjectDevelopmentLease)
            .where(ProjectDevelopmentLease.project_id == project_id)
            .with_for_update()
        )
        if lease is None:
            return False
        if (
            lease.lease_owner is not None
            and lease.lease_owner != owner
            and lease.lease_expires_at is not None
            and lease.lease_expires_at > now
        ):
            return False
        lease.lease_owner = owner
        lease.lease_expires_at = now + timedelta(
            seconds=self.settings.development_project_lease_seconds
        )
        lease.updated_at = now
        return True

    async def _release_project_lease(self, task: Task | None, owner: str) -> None:
        if (
            task is None
            or task.project_id is None
            or task.kind
            not in (
                TaskKind.DEVELOPMENT,
                TaskKind.QA,
            )
        ):
            return
        lease = await self.session.scalar(
            select(ProjectDevelopmentLease)
            .where(ProjectDevelopmentLease.project_id == task.project_id)
            .with_for_update()
        )
        if lease is not None and lease.lease_owner == owner:
            lease.lease_owner = None
            lease.lease_expires_at = None
            lease.updated_at = datetime.now(UTC)

    async def _cancel_locked_job(self, job: ExecutionJob, code: str, message: str) -> None:
        job.status = ExecutionJobStatus.CANCELLED
        job.completed_at = datetime.now(UTC)
        job.last_error = {"code": code, "message": message}

    async def _fail_locked_job(self, job: ExecutionJob, code: str, message: str) -> None:
        job.status = ExecutionJobStatus.FAILED
        job.completed_at = datetime.now(UTC)
        job.last_error = {"code": code, "message": message}

    @staticmethod
    def _phase_record(phase: ExecutionPhase, attempt: int, evidence: dict | None = None) -> dict:
        record: dict = {
            "phase": phase.value,
            "attempt": attempt,
            "at": datetime.now(UTC).isoformat(),
        }
        if evidence:
            record["evidence"] = evidence
        return record

    @classmethod
    def _bounded_evidence(cls, value: dict) -> dict:
        """Keep lifecycle evidence intentionally small and free of source/log payloads."""
        allowed = {
            "repository_initialized",
            "repository_branch",
            "initial_checkpoint_created",
            "project_type",
            "package_manager",
            "changed_files_count",
            "available_actions",
            "working_tree",
            "result",
            "reason",
        }
        bounded: dict = {}
        for key, item in value.items():
            if key not in allowed:
                continue
            if isinstance(item, str):
                bounded[key] = item[:1000]
            elif isinstance(item, list):
                bounded[key] = item[:50]
            elif isinstance(item, dict):
                bounded[key] = cls._bounded_evidence(item)
            elif isinstance(item, bool | int | float) or item is None:
                bounded[key] = item
        return bounded

    async def _normalized_failure(
        self,
        job: ExecutionJob,
        task: Task | None,
        code: str | None,
        message: str | None,
        task_run_id: UUID | None = None,
    ) -> dict:
        normalized_code = code or "UNCLASSIFIED_RUNTIME_FAILURE"
        normalized_message = (
            message[:4000]
            if message
            else "Forge recorded a runtime failure without a classified error message."
        )
        upper = normalized_code.upper()
        if upper in {
            "DUPLICATE_TOOL_LOOP",
            "DEVELOPMENT_STAGNATION",
            "TOOL_ARGUMENT_REPAIR_LIMIT_EXHAUSTED",
        }:
            category = "TOOL_OR_AGENT_RUNTIME_FAILURE"
        elif "CHECKPOINT" in upper or upper.startswith("GIT_"):
            category = "CHECKPOINT_FAILURE"
        elif upper.startswith("DEVELOPMENT_") or "RUNNER" in upper or "LEASE" in upper:
            category = "INFRASTRUCTURE_FAILURE"
        elif upper == "MODEL_CAPABILITY_UNAVAILABLE":
            category = "PROVIDER_CAPABILITY_MISMATCH"
        elif (upper.startswith("TASK_") and "BUDGET" in upper) or upper == (
            "MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED"
        ):
            category = "TASK_BUDGET_EXHAUSTION"
        elif upper in {
            "MODEL_CONTEXT_LIMIT_EXCEEDED",
            "MODEL_CONTEXT_ROLLOVER_LIMIT_EXHAUSTED",
        }:
            category = "MODEL_CONTEXT_EXHAUSTION"
        elif upper in {
            "PROVIDER_REQUEST_INVALID",
            "PROVIDER_RESPONSE_INVALID",
            "PROVIDER_TOOL_SCHEMA_INVALID",
            "PROVIDER_TOOL_PROTOCOL_ERROR",
        }:
            category = "PROVIDER_OR_MODEL_FAILURE"
        elif upper.startswith("MODEL_") or upper in {
            "INVALID_MODEL_OUTPUT",
            "MODEL_BUDGET_EXHAUSTED",
        }:
            category = "PROVIDER_OR_MODEL_FAILURE"
        elif "TOOL" in upper or upper == "DUPLICATE_TOOL_LOOP":
            category = "TOOL_OR_AGENT_RUNTIME_FAILURE"
        elif "TIMEOUT" in upper or "TIMED_OUT" in upper:
            category = "TIMEOUT"
        elif "CANCEL" in upper:
            category = "CANCELLATION"
        elif upper.startswith("QA_") or "VERIFICATION" in upper:
            category = "VERIFICATION_OR_QA_FAILURE"
        else:
            category = "UNCLASSIFIED_RUNTIME_FAILURE"

        run_id = task_run_id or job.task_run_id
        if run_id is None:
            run_id = await self.session.scalar(
                select(TaskRun.id)
                .where(TaskRun.task_id == job.task_id)
                .order_by(TaskRun.created_at.desc())
                .limit(1)
            )
        command = await self.session.scalar(
            select(DevelopmentExecution)
            .where(
                DevelopmentExecution.task_id == job.task_id,
                DevelopmentExecution.status.notin_(
                    ("SUCCEEDED", "RUNNING", "AUTHORIZED", "REQUESTED")
                ),
            )
            .order_by(DevelopmentExecution.created_at.desc())
            .limit(1)
        )
        tool = await self.session.scalar(
            select(ToolCall)
            .where(
                ToolCall.task_id == job.task_id,
                ToolCall.status.in_(("FAILED", "DENIED", "CANCELLED")),
            )
            .order_by(ToolCall.created_at.desc())
            .limit(1)
        )
        agent_run = await self.session.scalar(
            select(AgentRun)
            .where(AgentRun.task_id == job.task_id, AgentRun.error_code.is_not(None))
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        latest_model_run = await self.session.scalar(
            select(AgentRun)
            .where(AgentRun.task_id == job.task_id)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        budget = await self.session.scalar(
            select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == job.task_id)
        )
        metric = await self.session.scalar(
            select(TaskRuntimeMetric).where(TaskRuntimeMetric.task_id == job.task_id)
        )
        profile = (
            await self.session.scalar(
                select(ProjectDevelopmentProfile).where(
                    ProjectDevelopmentProfile.project_id == task.project_id
                )
            )
            if task is not None and task.project_id is not None
            else None
        )
        rollover = await self.session.scalar(
            select(Event)
            .where(Event.task_id == job.task_id, Event.type == "DEV_CONTEXT_ROLLOVER")
            .order_by(Event.created_at.desc())
            .limit(1)
        )
        rollovers = list(
            await self.session.scalars(
                select(Event)
                .where(Event.task_id == job.task_id, Event.type == "DEV_CONTEXT_ROLLOVER")
                .order_by(Event.created_at)
            )
        )
        working_tree = dict(job.working_tree_state)
        if profile is not None:
            prior_count = working_tree.get("changed_files_count", 0)
            working_tree["changed_files_count"] = max(
                prior_count if isinstance(prior_count, int) else 0,
                profile.changed_files_count,
            )
            working_tree["initial_checkpoint_created"] = profile.initial_checkpoint_created
        job.working_tree_state = working_tree
        diagnostics = (
            {
                "input_tokens_consumed": budget.consumed_input_tokens,
                "cached_input_tokens": budget.consumed_cached_tokens,
                "input_token_budget": budget.max_input_tokens,
                "remaining_input_tokens": max(
                    budget.max_input_tokens - budget.consumed_input_tokens, 0
                ),
                "model_calls": budget.consumed_model_calls,
                "selected_model": latest_model_run.model_id if latest_model_run else None,
                "selected_provider": latest_model_run.provider if latest_model_run else None,
                "last_request_input_tokens": (
                    latest_model_run.input_tokens if latest_model_run else 0
                ),
                "model_context_limit": (
                    rollover.details.get("model_context_limit")
                    if rollover is not None
                    else self.settings.forge_dev_context_limit
                ),
                "rollover_threshold": (
                    rollover.details.get("rollover_threshold")
                    if rollover is not None
                    else int(
                        self.settings.forge_dev_context_limit
                        * self.settings.forge_dev_context_checkpoint_ratio
                    )
                ),
                "context_bytes_sent": metric.context_bytes_sent if metric else 0,
                "rollover_count": len(rollovers),
                "rollover_limit": self.settings.forge_dev_max_rollovers,
                "rollover_reasons": [
                    {
                        "count": item.details.get("rollover_count"),
                        "reason": item.details.get("reason"),
                        "estimated_input_tokens": item.details.get("estimated_input_tokens"),
                        "progress": item.details.get("progress_since_last_rollover", []),
                    }
                    for item in rollovers
                ],
                "last_rollover": rollover.details if rollover is not None else None,
                "context_component_bytes": (metric.context_component_bytes if metric else {}),
                "cached_observations_reused": (metric.reused_observations if metric else 0),
                "stale_cache_invalidations": (
                    metric.stale_observation_invalidations if metric else 0
                ),
                "malformed_tool_repair_attempts": (metric.malformed_tool_repairs if metric else 0),
                "stagnation_signals": metric.stagnation_signals if metric else 0,
                "last_useful_action": metric.last_useful_action if metric else {},
                "rollover_disposition": (
                    "ROLLOVER_RECORDED"
                    if rollover is not None
                    else "NO_ROLLOVER_RECORDED_BEFORE_STOP"
                ),
            }
            if budget is not None
            else None
        )
        return {
            "category": category,
            "code": normalized_code,
            "message": normalized_message,
            "phase": job.phase.value,
            "failed_at": datetime.now(UTC).isoformat(),
            "execution_job_id": str(job.id),
            "task_id": str(job.task_id),
            "task_run_id": str(run_id) if run_id else None,
            "agent_id": str(job.agent_id),
            "agent_role": task.kind.value if task is not None else None,
            "worker_id": str(job.worker_id) if job.worker_id else None,
            "attempt": job.attempts,
            "command": (
                {
                    "id": str(command.id),
                    "action": command.action.value,
                    "status": command.status.value,
                    "exit_code": command.exit_code,
                    "error_code": command.error_code,
                }
                if command is not None
                else None
            ),
            "tool": (
                {
                    "id": str(tool.id),
                    "name": tool.tool_name,
                    "status": tool.status.value,
                    "error": tool.error,
                }
                if tool is not None
                else None
            ),
            "agent_run": (
                {
                    "id": str(agent_run.id),
                    "provider": agent_run.provider,
                    "model": agent_run.model_alias,
                    "error_code": agent_run.error_code,
                }
                if agent_run is not None
                else None
            ),
            "environment": job.environment_state,
            "checkpoint": job.checkpoint_state,
            "working_tree": working_tree,
            "budget_diagnostics": diagnostics,
        }
