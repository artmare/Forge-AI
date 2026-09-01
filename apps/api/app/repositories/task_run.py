from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskRunStatus
from app.domain.models import TaskRun


class TaskRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, task_run: TaskRun) -> TaskRun:
        self.session.add(task_run)
        await self.session.flush()
        return task_run

    async def get(self, task_run_id: UUID) -> TaskRun | None:
        return await self.session.get(TaskRun, task_run_id)

    async def get_active_for_task(self, task_id: UUID) -> TaskRun | None:
        return await self.session.scalar(
            select(TaskRun)
            .where(TaskRun.task_id == task_id, TaskRun.status == TaskRunStatus.STARTED)
            .with_for_update()
        )

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[TaskRun]:
        result = await self.session.scalars(
            select(TaskRun)
            .where(TaskRun.task_id == task_id)
            .order_by(TaskRun.iteration, TaskRun.created_at)
            .offset(offset)
            .limit(limit)
        )
        return list(result)
