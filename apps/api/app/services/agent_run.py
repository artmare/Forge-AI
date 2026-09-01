from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.domain.models import AgentRun
from app.repositories.agent import AgentRepository
from app.repositories.agent_run import AgentRunRepository, AgentRunStatsRecord
from app.repositories.task import TaskRepository


class AgentRunService:
    def __init__(self, session: AsyncSession) -> None:
        self.runs = AgentRunRepository(session)
        self.tasks = TaskRepository(session)
        self.agents = AgentRepository(session)

    async def get(self, run_id: UUID) -> AgentRun:
        run = await self.runs.get(run_id)
        if run is None:
            raise EntityNotFoundError("Agent run")
        return run

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[AgentRun]:
        if await self.tasks.get(task_id) is None:
            raise EntityNotFoundError("Task")
        return await self.runs.list_for_task(task_id, offset, limit)

    async def list_recent(self, offset: int = 0, limit: int = 20) -> list[dict[str, object]]:
        return await self.runs.list_recent(offset, limit)

    async def stats(self, agent_id: UUID | None = None) -> AgentRunStatsRecord:
        if agent_id is not None and await self.agents.get(agent_id) is None:
            raise EntityNotFoundError("Agent")
        return await self.runs.stats(agent_id)
