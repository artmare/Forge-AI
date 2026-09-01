from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ExecutionJobStatus
from app.domain.models import ExecutionJob


@dataclass(frozen=True)
class ExecutionJobStatsRecord:
    total: int
    by_status: dict[ExecutionJobStatus, int]
    average_execution_duration_ms: float | None


class ExecutionJobRepository:
    ACTIVE_STATUSES = (
        ExecutionJobStatus.PENDING,
        ExecutionJobStatus.CLAIMED,
        ExecutionJobStatus.RUNNING,
    )
    EXECUTING_STATUSES = (ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: ExecutionJob) -> ExecutionJob:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: UUID) -> ExecutionJob | None:
        return await self.session.get(ExecutionJob, job_id)

    async def get_for_update(self, job_id: UUID) -> ExecutionJob | None:
        return await self.session.scalar(
            select(ExecutionJob).where(ExecutionJob.id == job_id).with_for_update()
        )

    async def get_active_for_task(self, task_id: UUID) -> ExecutionJob | None:
        return await self.session.scalar(
            select(ExecutionJob).where(
                ExecutionJob.task_id == task_id,
                ExecutionJob.status.in_(self.ACTIVE_STATUSES),
            )
        )

    async def list(
        self,
        *,
        task_id: UUID | None = None,
        agent_id: UUID | None = None,
        worker_id: UUID | None = None,
        status: ExecutionJobStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ExecutionJob]:
        statement = select(ExecutionJob)
        if task_id is not None:
            statement = statement.where(ExecutionJob.task_id == task_id)
        if agent_id is not None:
            statement = statement.where(ExecutionJob.agent_id == agent_id)
        if worker_id is not None:
            statement = statement.where(ExecutionJob.worker_id == worker_id)
        if status is not None:
            statement = statement.where(ExecutionJob.status == status)
        rows = await self.session.scalars(
            statement.order_by(ExecutionJob.created_at.desc(), ExecutionJob.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(rows)

    async def count_executing(self) -> int:
        return int(
            await self.session.scalar(
                select(func.count(ExecutionJob.id)).where(
                    ExecutionJob.status.in_(self.EXECUTING_STATUSES)
                )
            )
            or 0
        )

    async def stats(self) -> ExecutionJobStatsRecord:
        duration = func.extract("epoch", ExecutionJob.completed_at - ExecutionJob.started_at) * 1000
        total, average = (
            await self.session.execute(
                select(
                    func.count(ExecutionJob.id),
                    func.avg(case((ExecutionJob.started_at.is_not(None), duration), else_=None)),
                )
            )
        ).one()
        rows = await self.session.execute(
            select(ExecutionJob.status, func.count(ExecutionJob.id)).group_by(ExecutionJob.status)
        )
        by_status = {status: 0 for status in ExecutionJobStatus}
        by_status.update({status: count for status, count in rows})
        return ExecutionJobStatsRecord(
            total=total,
            by_status=by_status,
            average_execution_duration_ms=float(average) if average is not None else None,
        )

    async def claim_expired(self, now: datetime, limit: int = 100) -> list[ExecutionJob]:
        rows = await self.session.scalars(
            select(ExecutionJob)
            .where(
                ExecutionJob.status.in_(self.EXECUTING_STATUSES),
                ExecutionJob.lease_expires_at < now,
            )
            .order_by(ExecutionJob.lease_expires_at, ExecutionJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(rows)
