from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskStatus
from app.domain.models import Task


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, task: Task) -> Task:
        self.session.add(task)
        await self.session.flush()
        return task

    async def get(self, task_id: UUID) -> Task | None:
        return await self.session.get(Task, task_id)

    async def get_current(self, task_id: UUID) -> Task | None:
        return await self.session.scalar(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )

    async def get_for_update(self, task_id: UUID) -> Task | None:
        return await self.session.scalar(select(Task).where(Task.id == task_id).with_for_update())

    async def list(
        self,
        *,
        company_id: UUID | None = None,
        project_id: UUID | None = None,
        assigned_agent_id: UUID | None = None,
        status: TaskStatus | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Task]:
        statement = select(Task)
        if company_id is not None:
            statement = statement.where(Task.company_id == company_id)
        if project_id is not None:
            statement = statement.where(Task.project_id == project_id)
        if assigned_agent_id is not None:
            statement = statement.where(Task.assigned_agent_id == assigned_agent_id)
        if status is not None:
            statement = statement.where(Task.status == status)
        result = await self.session.scalars(
            statement.order_by(Task.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result)
