import asyncio
import logging
import os
import socket
from uuid import UUID

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.database import get_session_factory
from app.infrastructure.event_stream import RedisEventStream
from app.repositories.event_outbox import EventOutboxRepository
from app.repositories.event_worker_health import EventWorkerHealthRepository

logger = logging.getLogger(__name__)
configure_logging(get_settings().log_level, "forge-event-publisher")


class EventPublisher:
    def __init__(
        self, transport: RedisEventStream | None = None, worker_id: str | None = None
    ) -> None:
        self.settings = get_settings()
        self.transport = transport or RedisEventStream()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def claim(self) -> list[UUID]:
        async with get_session_factory()() as session:
            rows = await EventOutboxRepository(session).claim_batch(
                worker_id=self.worker_id,
                batch_size=self.settings.event_publish_batch_size,
                lease_seconds=self.settings.event_processing_lease_seconds,
            )
            ids = [row.id for row in rows]
            await EventWorkerHealthRepository(session).heartbeat("publisher")
            await session.commit()
            if ids:
                logger.info(
                    "Outbox batch claimed",
                    extra={
                        "event": "outbox_claimed",
                        "worker_id": self.worker_id,
                        "outbox_ids": [str(row_id) for row_id in ids],
                    },
                )
            return ids

    async def publish_one(self, outbox_id: UUID) -> bool:
        async with get_session_factory()() as session:
            row = await EventOutboxRepository(session).get(outbox_id)
            if row is None:
                return False
            event_id, topic, payload, attempts = (
                row.event_id,
                row.topic,
                row.payload,
                row.attempts,
            )

        try:
            redis_message_id = await self.transport.publish(topic, payload)
        except Exception as exc:
            delay = min(
                self.settings.event_retry_base_seconds * (2 ** max(attempts - 1, 0)),
                60.0,
            )
            async with get_session_factory()() as session:
                repo = EventOutboxRepository(session)
                row = await repo.get(outbox_id)
                if row is not None:
                    await repo.mark_failed(row, f"{type(exc).__name__}: {exc}", delay)
                await EventWorkerHealthRepository(session).heartbeat(
                    "publisher", f"{type(exc).__name__}: {exc}"
                )
                await session.commit()
            logger.warning(
                "Event publication failed",
                extra={
                    "event": "event_publish_failed",
                    "event_id": str(event_id),
                    "outbox_id": str(outbox_id),
                },
                exc_info=True,
            )
            return False

        async with get_session_factory()() as session:
            repo = EventOutboxRepository(session)
            row = await repo.get(outbox_id)
            if row is not None:
                await repo.mark_published(row)
            await EventWorkerHealthRepository(session).heartbeat("publisher")
            await session.commit()
        logger.info(
            "Event published",
            extra={
                "event": "event_published",
                "event_id": str(event_id),
                "outbox_id": str(outbox_id),
                "redis_message_id": redis_message_id,
            },
        )
        return True

    async def process_batch(self) -> int:
        outbox_ids = await self.claim()
        for outbox_id in outbox_ids:
            await self.publish_one(outbox_id)
        return len(outbox_ids)

    async def run(self) -> None:
        interval = self.settings.event_publish_interval_ms / 1_000
        logger.info("Event publisher started", extra={"event": "event_publisher_started"})
        while True:
            try:
                count = await self.process_batch()
                if count == 0:
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Event publisher loop failed", extra={"event": "event_publisher_loop_failed"}
                )
                await asyncio.sleep(interval)


async def main() -> None:
    await EventPublisher().run()


if __name__ == "__main__":
    asyncio.run(main())
