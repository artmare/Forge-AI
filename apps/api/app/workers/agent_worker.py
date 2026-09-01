import asyncio
import logging
import signal
import traceback
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from app.agent_runtime.runtime import AgentRuntime
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import ExecutionPhase, TaskKind, TaskStatus, WorkerStatus
from app.domain.exceptions import DevelopmentInfrastructureError, DomainError
from app.domain.models import Agent, ExecutionJob, Task
from app.infrastructure.database import close_database, get_session_factory
from app.orchestration.worker_service import WorkerExecutionService

settings = get_settings()
configure_logging(settings.log_level, "forge-agent-worker")
logger = logging.getLogger(__name__)
HEALTH_ID_PATH = Path("/tmp/forge-agent-worker-id")


class AgentWorker:
    def __init__(self) -> None:
        self.worker_key = f"worker-{uuid4().hex}"
        self.worker_id: UUID | None = None
        self.stop_event = asyncio.Event()
        self.lease_owner = self.worker_key

    async def run(self) -> None:
        async with get_session_factory()() as session:
            worker = await WorkerExecutionService(session).register(
                self.worker_key, settings.agent_worker_concurrency
            )
            self.worker_id = worker.id
            HEALTH_ID_PATH.write_text(str(worker.id), encoding="utf-8")
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        slots = [
            asyncio.create_task(self._slot_loop(slot))
            for slot in range(settings.agent_worker_concurrency)
        ]
        logger.info(
            "Agent worker started",
            extra={
                "event": "agent_worker_started",
                "worker_id": str(self.worker_id),
                "concurrency": settings.agent_worker_concurrency,
            },
        )
        await self.stop_event.wait()
        await self._set_status(WorkerStatus.DRAINING)
        try:
            await asyncio.wait_for(
                asyncio.gather(*slots, return_exceptions=True),
                timeout=settings.worker_shutdown_grace_seconds,
            )
        except TimeoutError:
            for slot in slots:
                slot.cancel()
            await asyncio.gather(*slots, return_exceptions=True)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await self._set_status(WorkerStatus.OFFLINE)
        HEALTH_ID_PATH.unlink(missing_ok=True)
        await close_database()

    def stop(self) -> None:
        self.stop_event.set()

    async def _slot_loop(self, slot: int) -> None:
        interval = settings.worker_poll_interval_ms / 1000
        consecutive_failures = 0
        while not self.stop_event.is_set():
            try:
                job_id = await self._claim()
                consecutive_failures = 0
                if job_id is None:
                    await asyncio.sleep(interval)
                    continue
                await self._execute(job_id, slot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                    logger.warning(
                        "Agent worker claim unavailable; retrying with backoff",
                        extra={
                            "event": "agent_worker_claim_unavailable",
                            "slot": slot,
                            "consecutive_failures": consecutive_failures,
                            "error_type": type(exc).__name__,
                        },
                    )
                delay = min(interval * (2 ** min(consecutive_failures, 4)), 5.0)
                await asyncio.sleep(delay)

    async def _claim(self) -> UUID | None:
        if self.worker_id is None:
            return None
        async with get_session_factory()() as session:
            job = await WorkerExecutionService(session).claim(self.worker_id, self.lease_owner)
            return job.id if job is not None else None

    async def _execute(self, job_id: UUID, slot: int) -> None:
        async with get_session_factory()() as session:
            job = await WorkerExecutionService(session).start(job_id, self.lease_owner)
        if job is None:
            return
        lease_healthy = asyncio.Event()
        lease_healthy.set()
        renewer = asyncio.create_task(self._lease_loop(job_id, lease_healthy))
        cancelled_by_task = asyncio.Event()
        run = None
        runtime_task = asyncio.create_task(
            self._run_execution(job, lease_healthy),
            name=f"forge-execution-{job.id}",
        )
        cancellation_monitor = asyncio.create_task(
            self._task_cancellation_loop(job.task_id, runtime_task, cancelled_by_task),
            name=f"forge-cancellation-{job.id}",
        )
        try:
            run = await runtime_task
            async with get_session_factory()() as session:
                await WorkerExecutionService(session).succeed(
                    job_id, self.lease_owner, run.task_run_id
                )
            logger.info(
                "Execution job succeeded",
                extra={
                    "event": "execution_job_succeeded",
                    "worker_id": str(self.worker_id),
                    "execution_job_id": str(job_id),
                    "task_id": str(job.task_id),
                    "task_run_id": str(run.task_run_id),
                    "agent_id": str(job.agent_id),
                    "slot": slot,
                },
            )
        except asyncio.CancelledError:
            if not cancelled_by_task.is_set():
                raise
            await self._persist_failure(
                job_id,
                "AGENT_EXECUTION_CANCELLED",
                "Task was cancelled during development execution.",
                None,
            )
        except DevelopmentInfrastructureError as exc:
            await self._persist_infrastructure_failure(job_id, exc.code, exc.message)
        except DomainError as exc:
            await self._persist_failure(
                job_id,
                exc.code,
                exc.message,
                run.task_run_id if run is not None else None,
                internal_error={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "stack": traceback.format_exc(),
                },
            )
        except Exception as exc:
            logger.exception(
                "Execution job runtime failed",
                extra={
                    "event": "execution_job_runtime_failed",
                    "worker_id": str(self.worker_id),
                    "execution_job_id": str(job_id),
                    "task_id": str(job.task_id),
                    "agent_id": str(job.agent_id),
                    "error_type": type(exc).__name__,
                },
            )
            await self._persist_failure(
                job_id,
                "UNCLASSIFIED_RUNTIME_FAILURE",
                f"Unexpected {type(exc).__name__} in the agent worker runtime.",
                run.task_run_id if run is not None else None,
            )
        finally:
            renewer.cancel()
            cancellation_monitor.cancel()
            if not runtime_task.done():
                runtime_task.cancel()
            await asyncio.gather(
                renewer,
                cancellation_monitor,
                runtime_task,
                return_exceptions=True,
            )

    async def _run_execution(self, job: ExecutionJob, lease_healthy: asyncio.Event):
        async def execution_guard() -> bool:
            return lease_healthy.is_set()

        async with get_session_factory()() as session:
            agent = await session.get(Agent, job.agent_id)
            task = await session.get(Task, job.task_id)
            if task is not None and task.kind == TaskKind.DEVELOPMENT:
                await self._update_phase(job.id, ExecutionPhase.ENVIRONMENT_SETUP)
                bootstrap = ProjectBootstrapService(session)
                profile = await bootstrap.ensure_task(task.id)
                profile_evidence = {
                    "repository_initialized": profile.repository_initialized,
                    "repository_branch": profile.repository_branch,
                    "initial_checkpoint_created": profile.initial_checkpoint_created,
                    "project_type": profile.project_type.value,
                    "package_manager": profile.package_manager.value,
                    "changed_files_count": profile.changed_files_count,
                    "working_tree": {
                        "changed_files_count": profile.changed_files_count,
                    },
                    "available_actions": [
                        action.value
                        for action in (
                            profile.install_action,
                            profile.test_action,
                            profile.build_action,
                            profile.lint_action,
                            profile.typecheck_action,
                        )
                        if action is not None
                    ],
                }
                await self._update_phase(job.id, ExecutionPhase.ENVIRONMENT_SETUP, profile_evidence)
                await self._update_phase(job.id, ExecutionPhase.CHECKPOINTING)
                profile = await bootstrap.ensure_task(task.id, checkpoint=True)
                profile_evidence.update(
                    {
                        "initial_checkpoint_created": profile.initial_checkpoint_created,
                        "changed_files_count": profile.changed_files_count,
                        "working_tree": {
                            "changed_files_count": profile.changed_files_count,
                        },
                    }
                )
                await self._update_phase(job.id, ExecutionPhase.CHECKPOINTING, profile_evidence)
            model_alias = (
                str(agent.configuration.get("model_alias", "default"))
                if agent is not None
                else "default"
            )
            await self._update_phase(job.id, ExecutionPhase.AGENT_START)
            await self._update_phase(job.id, ExecutionPhase.EXECUTING)
            run = await AgentRuntime(session, execution_guard=execution_guard).execute(
                job.task_id,
                model_alias,
                defer_review=task is not None and task.kind == TaskKind.DEVELOPMENT,
            )
            if task is not None and task.kind == TaskKind.DEVELOPMENT:
                await self._update_phase(job.id, ExecutionPhase.VERIFYING)
                await self._update_phase(job.id, ExecutionPhase.QA)
                await DevelopmentWorkflowService(session).finalize(job.task_id, run.task_run_id)
            await self._update_phase(job.id, ExecutionPhase.FINALIZING)
            return run

    async def _update_phase(
        self,
        job_id: UUID,
        phase: ExecutionPhase,
        evidence: dict | None = None,
    ) -> None:
        async with get_session_factory()() as phase_session:
            updated = await WorkerExecutionService(phase_session).update_phase(
                job_id, self.lease_owner, phase, evidence
            )
        if not updated:
            raise DevelopmentInfrastructureError(
                "EXECUTION_PHASE_PERSIST_FAILED",
                f"Forge could not durably enter execution phase {phase.value}.",
            )

    async def _task_cancellation_loop(
        self,
        task_id: UUID,
        runtime_task: asyncio.Task,
        cancelled_by_task: asyncio.Event,
    ) -> None:
        interval = max(settings.worker_poll_interval_ms / 1000, 0.1)
        while not runtime_task.done():
            await asyncio.sleep(interval)
            async with get_session_factory()() as session:
                status = await session.scalar(select(Task.status).where(Task.id == task_id))
            if status == TaskStatus.CANCELLED:
                cancelled_by_task.set()
                runtime_task.cancel()
                return

    async def _persist_failure(
        self,
        job_id: UUID,
        code: str,
        message: str,
        task_run_id: UUID | None,
        internal_error: dict | None = None,
    ) -> None:
        try:
            async with get_session_factory()() as session:
                await WorkerExecutionService(session).fail(
                    job_id,
                    self.lease_owner,
                    code,
                    message,
                    task_run_id,
                    internal_error,
                )
        except Exception:
            logger.exception(
                "Execution failure could not be persisted; lease recovery will reconcile",
                extra={
                    "event": "execution_job_failure_persist_failed",
                    "execution_job_id": str(job_id),
                    "worker_id": str(self.worker_id),
                },
            )

    async def _persist_infrastructure_failure(self, job_id: UUID, code: str, message: str) -> None:
        try:
            async with get_session_factory()() as session:
                await WorkerExecutionService(session).retry_infrastructure(
                    job_id, self.lease_owner, code, message
                )
        except Exception:
            logger.exception(
                "Infrastructure retry could not be persisted; lease recovery will reconcile",
                extra={
                    "event": "execution_infrastructure_retry_failed",
                    "execution_job_id": str(job_id),
                    "worker_id": str(self.worker_id),
                },
            )

    async def _lease_loop(self, job_id: UUID, lease_healthy: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(settings.job_lease_renew_interval_seconds)
            try:
                async with get_session_factory()() as session:
                    renewed = await WorkerExecutionService(session).renew_lease(
                        job_id, self.lease_owner
                    )
                if not renewed:
                    lease_healthy.clear()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                lease_healthy.clear()
                logger.exception(
                    "Execution lease renewal failed; future runtime steps are blocked",
                    extra={
                        "event": "execution_job_lease_renew_failed",
                        "execution_job_id": str(job_id),
                        "worker_id": str(self.worker_id),
                    },
                )
                return

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.worker_heartbeat_interval_seconds)
            if self.worker_id is None:
                continue
            try:
                async with get_session_factory()() as session:
                    if not await WorkerExecutionService(session).heartbeat(self.worker_id):
                        self.stop()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Worker heartbeat failed",
                    extra={
                        "event": "worker_heartbeat_failed",
                        "worker_id": str(self.worker_id),
                    },
                )

    async def _set_status(self, status: WorkerStatus) -> None:
        if self.worker_id is None:
            return
        try:
            async with get_session_factory()() as session:
                await WorkerExecutionService(session).set_status(self.worker_id, status)
        except Exception:
            logger.exception(
                "Worker status update failed",
                extra={
                    "event": "worker_status_update_failed",
                    "worker_id": str(self.worker_id),
                    "status": status.value,
                },
            )


async def main() -> None:
    worker = AgentWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
