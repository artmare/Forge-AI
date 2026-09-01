from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import EventDeadLetter


class EventDeadLetterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_once(self, row: EventDeadLetter) -> EventDeadLetter:
        now = datetime.now(UTC)
        statement = (
            insert(EventDeadLetter)
            .values(
                id=row.id or uuid4(),
                event_id=row.event_id,
                consumer=row.consumer,
                event_type=row.event_type,
                original_event=row.original_event,
                failure_reason=row.failure_reason,
                attempts=row.attempts,
                redis_message_id=row.redis_message_id,
                failed_at=row.failed_at or now,
                created_at=row.created_at or now,
            )
            .on_conflict_do_update(
                constraint="uq_event_dead_letters_event_consumer",
                set_={
                    "failure_reason": row.failure_reason,
                    "attempts": row.attempts,
                    "redis_message_id": row.redis_message_id,
                    "failed_at": row.failed_at,
                },
            )
            .returning(EventDeadLetter)
        )
        return (await self.session.execute(statement)).scalar_one()

    async def list(
        self,
        *,
        consumer: str | None = None,
        event_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[EventDeadLetter]:
        statement = select(EventDeadLetter)
        if consumer is not None:
            statement = statement.where(EventDeadLetter.consumer == consumer)
        if event_type is not None:
            statement = statement.where(EventDeadLetter.event_type == event_type)
        rows = await self.session.scalars(
            statement.order_by(EventDeadLetter.failed_at.desc()).offset(offset).limit(limit)
        )
        return list(rows)

    async def count(self) -> int:
        return await self.session.scalar(select(func.count()).select_from(EventDeadLetter)) or 0
