from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Event


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event: Event) -> Event:
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_recent(self, company_id: UUID | None = None, limit: int = 5) -> list[Event]:
        statement = select(Event)
        if company_id is not None:
            statement = statement.where(Event.company_id == company_id)
        result = await self.session.scalars(
            statement.order_by(Event.created_at.desc()).limit(limit)
        )
        return list(result)

    async def get(self, event_id: UUID) -> Event | None:
        return await self.session.get(Event, event_id)

    async def list(
        self,
        *,
        company_id: UUID | None = None,
        project_id: UUID | None = None,
        agent_id: UUID | None = None,
        task_id: UUID | None = None,
        event_type: str | None = None,
        topic: str | None = None,
        correlation_id: UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Event]:
        statement = select(Event)
        filters = (
            (Event.company_id, company_id),
            (Event.project_id, project_id),
            (Event.agent_id, agent_id),
            (Event.task_id, task_id),
            (Event.type, event_type),
            (Event.topic, topic),
            (Event.correlation_id, correlation_id),
        )
        for column, value in filters:
            if value is not None:
                statement = statement.where(column == value)
        if date_from is not None:
            statement = statement.where(Event.created_at >= date_from)
        if date_to is not None:
            statement = statement.where(Event.created_at <= date_to)
        result = await self.session.scalars(
            statement.order_by(Event.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result)
