from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ExecutionJobStatus, WorkerStatus
from app.domain.models import ExecutionJob, WorkerNode


class WorkerNodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, worker: WorkerNode) -> WorkerNode:
        self.session.add(worker)
        await self.session.flush()
        return worker

    async def get(self, worker_id: UUID) -> WorkerNode | None:
        return await self.session.get(WorkerNode, worker_id)

    async def get_for_update(self, worker_id: UUID) -> WorkerNode | None:
        return await self.session.scalar(
            select(WorkerNode).where(WorkerNode.id == worker_id).with_for_update()
        )

    async def list(self, offset: int = 0, limit: int = 100) -> list[tuple[WorkerNode, int]]:
        active_count = (
            select(func.count(ExecutionJob.id))
            .where(
                ExecutionJob.worker_id == WorkerNode.id,
                ExecutionJob.status.in_((ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)),
            )
            .correlate(WorkerNode)
            .scalar_subquery()
        )
        rows = await self.session.execute(
            select(WorkerNode, active_count.label("active_jobs"))
            .order_by(WorkerNode.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(worker, active_jobs) for worker, active_jobs in rows]

    async def get_with_active_count(self, worker_id: UUID) -> tuple[WorkerNode, int] | None:
        active_count = (
            select(func.count(ExecutionJob.id))
            .where(
                ExecutionJob.worker_id == WorkerNode.id,
                ExecutionJob.status.in_((ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)),
            )
            .correlate(WorkerNode)
            .scalar_subquery()
        )
        row = (
            await self.session.execute(
                select(WorkerNode, active_count.label("active_jobs")).where(
                    WorkerNode.id == worker_id
                )
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def claim_stale(self, cutoff: datetime, limit: int = 100) -> list[WorkerNode]:
        rows = await self.session.scalars(
            select(WorkerNode)
            .where(
                WorkerNode.status.in_((WorkerStatus.ONLINE, WorkerStatus.DRAINING)),
                WorkerNode.last_heartbeat_at < cutoff,
            )
            .order_by(WorkerNode.last_heartbeat_at, WorkerNode.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(rows)

    async def count_active_and_stale(self, stale_cutoff: datetime) -> tuple[int, int]:
        active = int(
            await self.session.scalar(
                select(func.count(WorkerNode.id)).where(
                    WorkerNode.status == WorkerStatus.ONLINE,
                    WorkerNode.last_heartbeat_at >= stale_cutoff,
                )
            )
            or 0
        )
        stale = int(
            await self.session.scalar(
                select(func.count(WorkerNode.id)).where(
                    WorkerNode.status.in_((WorkerStatus.ONLINE, WorkerStatus.DRAINING)),
                    WorkerNode.last_heartbeat_at < stale_cutoff,
                )
            )
            or 0
        )
        return active, stale
