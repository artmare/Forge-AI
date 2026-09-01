from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskStatus
from app.domain.models import Agent, Company, Task


@dataclass(frozen=True)
class SystemCounts:
    companies: int
    agents: int
    tasks: int
    task_states: dict[TaskStatus, int]


class SystemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def counts(self) -> SystemCounts:
        statement = select(
            select(func.count(Company.id)).scalar_subquery().label("companies"),
            select(func.count(Agent.id)).scalar_subquery().label("agents"),
            select(func.count(Task.id)).scalar_subquery().label("tasks"),
        )
        row = (await self.session.execute(statement)).one()
        state_rows = await self.session.execute(
            select(Task.status, func.count(Task.id)).group_by(Task.status)
        )
        task_states = {status: 0 for status in TaskStatus}
        task_states.update({status: count for status, count in state_rows})
        return SystemCounts(
            companies=row.companies,
            agents=row.agents,
            tasks=row.tasks,
            task_states=task_states,
        )
