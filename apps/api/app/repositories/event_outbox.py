from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import OutboxStatus
from app.domain.models import EventOutbox


class EventOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: EventOutbox) -> EventOutbox:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def get(self, outbox_id: UUID) -> EventOutbox | None:
        return await self.session.get(EventOutbox, outbox_id)

    async def latest_for_event(self, event_id: UUID) -> EventOutbox | None:
        return await self.session.scalar(
            select(EventOutbox)
            .where(EventOutbox.event_id == event_id)
            .order_by(EventOutbox.created_at.desc())
            .limit(1)
        )

    async def list_for_event(self, event_id: UUID) -> list[EventOutbox]:
        rows = await self.session.scalars(
            select(EventOutbox)
            .where(EventOutbox.event_id == event_id)
            .order_by(EventOutbox.created_at.desc())
        )
        return list(rows)

    async def claim_batch(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
    ) -> list[EventOutbox]:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)
        publishable = or_(
            (
                EventOutbox.status.in_((OutboxStatus.PENDING, OutboxStatus.FAILED))
                & (EventOutbox.available_at <= now)
            ),
            (
                (EventOutbox.status == OutboxStatus.PROCESSING)
                & (EventOutbox.processing_started_at <= stale_before)
            ),
        )
        rows = list(
            await self.session.scalars(
                select(EventOutbox)
                .where(publishable)
                .order_by(EventOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
        )
        for row in rows:
            row.status = OutboxStatus.PROCESSING
            row.processing_started_at = now
            row.worker_id = worker_id
            row.attempts += 1
            row.last_error = None
        await self.session.flush()
        return rows

    async def mark_published(self, row: EventOutbox) -> None:
        row.status = OutboxStatus.PUBLISHED
        row.published_at = datetime.now(UTC)
        row.processing_started_at = None
        row.last_error = None
        await self.session.flush()

    async def mark_failed(self, row: EventOutbox, error: str, delay_seconds: float) -> None:
        row.status = OutboxStatus.FAILED
        row.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        row.processing_started_at = None
        row.last_error = error[:4000]
        await self.session.flush()

    async def status_counts(self) -> dict[OutboxStatus, int]:
        result = await self.session.execute(
            select(EventOutbox.status, func.count()).group_by(EventOutbox.status)
        )
        return {status: count for status, count in result.all()}
