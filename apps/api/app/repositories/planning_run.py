from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PlanningRunStatus
from app.domain.models import PlanningRun


class PlanningRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, run: PlanningRun) -> PlanningRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: UUID) -> PlanningRun | None:
        return await self.session.get(PlanningRun, run_id)

    async def latest_for_mission(
        self, mission_id: UUID, *, successful_only: bool = False
    ) -> PlanningRun | None:
        statement = select(PlanningRun).where(PlanningRun.mission_id == mission_id)
        if successful_only:
            statement = statement.where(PlanningRun.status == PlanningRunStatus.SUCCEEDED)
        return await self.session.scalar(
            statement.order_by(PlanningRun.created_at.desc(), PlanningRun.id.desc()).limit(1)
        )

    async def stale_running(self, cutoff: datetime, limit: int = 100) -> list[PlanningRun]:
        rows = await self.session.scalars(
            select(PlanningRun)
            .where(
                PlanningRun.status == PlanningRunStatus.RUNNING,
                PlanningRun.started_at < cutoff,
            )
            .order_by(PlanningRun.started_at, PlanningRun.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(rows)
