from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ToolCallStatus
from app.domain.models import Agent, Task, ToolCall


@dataclass(frozen=True)
class ToolCallStatsRecord:
    total: int
    by_status: dict[ToolCallStatus, int]
    average_duration_ms: float | None


class ToolCallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, call: ToolCall) -> ToolCall:
        self.session.add(call)
        await self.session.flush()
        return call

    async def get(self, call_id: UUID) -> ToolCall | None:
        return await self.session.get(ToolCall, call_id)

    async def get_for_update(self, call_id: UUID) -> ToolCall | None:
        return await self.session.scalar(
            select(ToolCall)
            .where(ToolCall.id == call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def list_for_task(
        self, task_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[ToolCall]:
        result = await self.session.scalars(
            select(ToolCall)
            .where(ToolCall.task_id == task_id)
            .order_by(ToolCall.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def list_for_agent_run(
        self, agent_run_id: UUID, offset: int = 0, limit: int = 100
    ) -> list[ToolCall]:
        result = await self.session.scalars(
            select(ToolCall)
            .where(ToolCall.agent_run_id == agent_run_id)
            .order_by(ToolCall.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def list_recent(self, offset: int = 0, limit: int = 20) -> list[dict[str, object]]:
        rows = await self.session.execute(
            select(ToolCall, Task.title, Agent.name)
            .join(Task, Task.id == ToolCall.task_id)
            .join(Agent, Agent.id == ToolCall.agent_id)
            .order_by(ToolCall.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            {"call": call, "task_title": task_title, "agent_name": agent_name}
            for call, task_title, agent_name in rows
        ]

    async def claim_stale(self, cutoff: datetime, limit: int = 100) -> list[ToolCall]:
        result = await self.session.scalars(
            select(ToolCall)
            .where(ToolCall.status == ToolCallStatus.RUNNING, ToolCall.started_at < cutoff)
            .order_by(ToolCall.started_at, ToolCall.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result)

    async def stats(self) -> ToolCallStatsRecord:
        duration_ms = func.extract("epoch", ToolCall.completed_at - ToolCall.started_at) * 1000
        aggregate = await self.session.execute(
            select(
                func.count(ToolCall.id),
                func.avg(case((ToolCall.started_at.is_not(None), duration_ms), else_=None)),
            )
        )
        total, average = aggregate.one()
        status_rows = await self.session.execute(
            select(ToolCall.status, func.count(ToolCall.id)).group_by(ToolCall.status)
        )
        by_status = {status: 0 for status in ToolCallStatus}
        by_status.update({status: count for status, count in status_rows})
        return ToolCallStatsRecord(
            total=total,
            by_status=by_status,
            average_duration_ms=float(average) if average is not None else None,
        )
