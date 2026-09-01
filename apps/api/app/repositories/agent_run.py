from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentRunStatus
from app.domain.models import Agent, AgentRun, Task


@dataclass(frozen=True)
class AgentRunStatsRecord:
    total: int
    by_status: dict[AgentRunStatus, int]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    estimated_cost: Decimal | None


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, run: AgentRun) -> AgentRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: UUID) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def get_for_update(self, run_id: UUID) -> AgentRun | None:
        return await self.session.scalar(
            select(AgentRun).where(AgentRun.id == run_id).with_for_update()
        )

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[AgentRun]:
        result = await self.session.scalars(
            select(AgentRun)
            .where(AgentRun.task_id == task_id)
            .order_by(AgentRun.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def list_recent(self, offset: int = 0, limit: int = 20) -> list[dict[str, object]]:
        rows = await self.session.execute(
            select(AgentRun, Task.title, Agent.name, Agent.role)
            .join(Task, Task.id == AgentRun.task_id)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .order_by(AgentRun.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "run": run,
                "task_title": task_title,
                "agent_name": agent_name,
                "agent_role": agent_role,
            }
            for run, task_title, agent_name, agent_role in rows
        ]

    async def claim_stale(self, cutoff: datetime, limit: int = 100) -> list[AgentRun]:
        result = await self.session.scalars(
            select(AgentRun)
            .where(
                AgentRun.status == AgentRunStatus.RUNNING,
                AgentRun.started_at < cutoff,
            )
            .order_by(AgentRun.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result)

    async def stats(self, agent_id: UUID | None = None) -> AgentRunStatsRecord:
        aggregate = select(
            func.count(AgentRun.id).label("total"),
            func.coalesce(func.sum(AgentRun.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AgentRun.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(AgentRun.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(AgentRun.total_tokens), 0).label("total_tokens"),
            func.sum(AgentRun.estimated_cost).label("estimated_cost"),
            func.count(AgentRun.estimated_cost).label("priced_count"),
        )
        status_query = select(AgentRun.status, func.count(AgentRun.id)).group_by(AgentRun.status)
        if agent_id is not None:
            aggregate = aggregate.where(AgentRun.agent_id == agent_id)
            status_query = status_query.where(AgentRun.agent_id == agent_id)
        row = (await self.session.execute(aggregate)).one()
        status_rows = await self.session.execute(status_query)
        by_status = {status: 0 for status in AgentRunStatus}
        by_status.update({status: count for status, count in status_rows})
        estimated_cost = (
            row.estimated_cost if row.total > 0 and row.priced_count == row.total else None
        )
        return AgentRunStatsRecord(
            total=row.total,
            by_status=by_status,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_tokens=row.cached_tokens,
            total_tokens=row.total_tokens,
            estimated_cost=estimated_cost,
        )
