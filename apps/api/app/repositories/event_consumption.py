from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EventConsumptionStatus
from app.domain.models import EventConsumption


class EventConsumptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, event_id: UUID, consumer: str) -> EventConsumption:
        await self.session.execute(
            insert(EventConsumption)
            .values(event_id=event_id, consumer=consumer)
            .on_conflict_do_nothing(constraint="uq_event_consumptions_event_consumer")
        )
        await self.session.flush()
        row = await self.session.scalar(
            select(EventConsumption).where(
                EventConsumption.event_id == event_id,
                EventConsumption.consumer == consumer,
            )
        )
        if row is None:  # pragma: no cover - protected by the unique insert/select
            raise RuntimeError("Could not persist event consumption")
        return row

    async def list_for_event(self, event_id: UUID) -> list[EventConsumption]:
        return list(
            await self.session.scalars(
                select(EventConsumption)
                .where(EventConsumption.event_id == event_id)
                .order_by(EventConsumption.consumer)
            )
        )

    async def claim_attempt(
        self, event_id: UUID, consumer: str, lease_seconds: int
    ) -> tuple[EventConsumption, bool]:
        row = await self.get_or_create(event_id, consumer)
        row = await self.session.scalar(
            select(EventConsumption).where(EventConsumption.id == row.id).with_for_update()
        )
        if row is None:  # pragma: no cover - protected by the prior insert/select
            raise RuntimeError("Could not lock event consumption")
        now = datetime.now(UTC)
        lease_active = (
            row.status == EventConsumptionStatus.PROCESSING
            and row.processing_started_at is not None
            and row.processing_started_at > now - timedelta(seconds=lease_seconds)
        )
        if row.status == EventConsumptionStatus.SUCCEEDED or lease_active:
            return row, False
        row.status = EventConsumptionStatus.PROCESSING
        row.processing_started_at = now
        row.attempt += 1
        row.error = None
        await self.session.flush()
        return row, True

    async def mark_retrying(self, row: EventConsumption, error: str) -> None:
        row.status = EventConsumptionStatus.RETRYING
        row.processing_started_at = None
        row.error = error[:4000]
        await self.session.flush()

    async def mark_succeeded(self, row: EventConsumption) -> None:
        row.status = EventConsumptionStatus.SUCCEEDED
        row.processing_started_at = None
        row.processed_at = datetime.now(UTC)
        row.error = None
        await self.session.flush()

    async def mark_failed(self, row: EventConsumption, error: str) -> None:
        row.status = EventConsumptionStatus.FAILED
        row.processing_started_at = None
        row.processed_at = datetime.now(UTC)
        row.error = error[:4000]
        await self.session.flush()
