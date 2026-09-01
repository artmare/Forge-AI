from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.domain.models import ToolCall
from app.repositories.agent_run import AgentRunRepository
from app.repositories.task import TaskRepository
from app.repositories.tool_call import ToolCallRepository, ToolCallStatsRecord


class ToolCallService:
    def __init__(self, session: AsyncSession) -> None:
        self.calls = ToolCallRepository(session)
        self.tasks = TaskRepository(session)
        self.agent_runs = AgentRunRepository(session)

    async def get(self, call_id: UUID) -> ToolCall:
        call = await self.calls.get(call_id)
        if call is None:
            raise EntityNotFoundError("Tool call")
        return call

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[ToolCall]:
        if await self.tasks.get(task_id) is None:
            raise EntityNotFoundError("Task")
        return await self.calls.list_for_task(task_id, offset, limit)

    async def list_for_agent_run(
        self, agent_run_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[ToolCall]:
        if await self.agent_runs.get(agent_run_id) is None:
            raise EntityNotFoundError("Agent run")
        return await self.calls.list_for_agent_run(agent_run_id, offset, limit)

    async def list_recent(self, offset: int = 0, limit: int = 20) -> list[dict[str, object]]:
        return await self.calls.list_recent(offset, limit)

    async def stats(self) -> ToolCallStatsRecord:
        return await self.calls.stats()
