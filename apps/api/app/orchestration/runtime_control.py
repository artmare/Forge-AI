from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import ExecutionJobStatus, TaskStatus
from app.domain.models import ExecutionJob, Task
from app.repositories.execution_job import ExecutionJobRepository
from app.repositories.runtime_control import RuntimeControlRepository
from app.repositories.worker_node import WorkerNodeRepository
from app.services.event_factory import EventFactory


class RuntimeControlService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = RuntimeControlRepository(session)
        self.jobs = ExecutionJobRepository(session)
        self.workers = WorkerNodeRepository(session)
        self.events = EventFactory(session)

    async def get_enabled(self) -> bool:
        control = await self.controls.ensure(self.settings.autonomy_enabled)
        return control.enabled

    async def set_enabled(self, enabled: bool) -> bool:
        try:
            control = await self.controls.get_for_update()
            if control is None:
                control = await self.controls.ensure(self.settings.autonomy_enabled)
            changed = control.enabled != enabled
            control.enabled = enabled
            if changed:
                await self.events.create(
                    event_type="ORCHESTRATOR_RESUMED" if enabled else "ORCHESTRATOR_PAUSED",
                    message=(
                        "Autonomous execution resumed."
                        if enabled
                        else "Autonomous execution paused."
                    ),
                    payload={"autonomy_enabled": enabled},
                )
            await self.session.commit()
            return changed
        except Exception:
            await self.session.rollback()
            raise

    async def status(self) -> dict[str, object]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.settings.worker_stale_seconds)
        control = await self.controls.ensure(self.settings.autonomy_enabled)
        stats = await self.jobs.stats()
        active_workers, stale_workers = await self.workers.count_active_and_stale(cutoff)
        queued_without_jobs = int(
            await self.session.scalar(
                select(func.count(Task.id)).where(
                    Task.status == TaskStatus.QUEUED,
                    ~select(ExecutionJob.id)
                    .where(
                        ExecutionJob.task_id == Task.id,
                        ExecutionJob.status.in_(ExecutionJobRepository.ACTIVE_STATUSES),
                    )
                    .exists(),
                )
            )
            or 0
        )
        return {
            "autonomy_enabled": control.enabled,
            "orchestrator_online": (
                control.last_reconciled_at is not None
                and control.last_reconciled_at
                >= now
                - timedelta(
                    seconds=max(self.settings.orchestrator_reconcile_interval_seconds * 3, 15)
                )
            ),
            "queued_jobs": stats.by_status[ExecutionJobStatus.PENDING],
            "running_jobs": (
                stats.by_status[ExecutionJobStatus.CLAIMED]
                + stats.by_status[ExecutionJobStatus.RUNNING]
            ),
            "failed_jobs": stats.by_status[ExecutionJobStatus.FAILED],
            "active_workers": active_workers,
            "stale_workers": stale_workers,
            "queued_tasks_without_jobs": queued_without_jobs,
            "last_reconciliation_time": control.last_reconciled_at,
            "total_jobs": stats.total,
            "job_counts": stats.by_status,
            "average_execution_duration_ms": stats.average_execution_duration_ms,
        }
