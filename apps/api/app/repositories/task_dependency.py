from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import TaskDependency


class TaskDependencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, dependency: TaskDependency) -> TaskDependency:
        self.session.add(dependency)
        await self.session.flush()
        return dependency

    async def for_task(self, task_id: UUID) -> list[TaskDependency]:
        rows = await self.session.scalars(
            select(TaskDependency)
            .where(TaskDependency.task_id == task_id)
            .order_by(TaskDependency.created_at, TaskDependency.id)
        )
        return list(rows)

    async def for_dependency(self, task_id: UUID) -> list[TaskDependency]:
        rows = await self.session.scalars(
            select(TaskDependency)
            .where(TaskDependency.depends_on_task_id == task_id)
            .order_by(TaskDependency.created_at, TaskDependency.id)
        )
        return list(rows)
