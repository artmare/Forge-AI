from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import EventWorkerHealth


class EventWorkerHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def heartbeat(self, worker: str, error: str | None = None) -> None:
        now = datetime.now(UTC)
        await self.session.execute(
            insert(EventWorkerHealth)
            .values(worker=worker, heartbeat_at=now, last_error=error)
            .on_conflict_do_update(
                constraint="uq_event_worker_health_worker",
                set_={"heartbeat_at": now, "last_error": error},
            )
        )

    async def get(self, worker: str) -> EventWorkerHealth | None:
        return await self.session.scalar(
            select(EventWorkerHealth).where(EventWorkerHealth.worker == worker)
        )
