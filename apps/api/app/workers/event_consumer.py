import asyncio
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.domain.models import EventDeadLetter
from app.infrastructure.database import get_session_factory
from app.infrastructure.event_stream import RedisEventStream
from app.repositories.event_consumption import EventConsumptionRepository
from app.repositories.event_dead_letter import EventDeadLetterRepository
from app.repositories.event_worker_health import EventWorkerHealthRepository
from app.schemas.event_system import EventEnvelope

logger = logging.getLogger(__name__)
configure_logging(get_settings().log_level, "forge-event-consumer")
EventHandler = Callable[[EventEnvelope], Awaitable[None]]


async def no_op_handler(envelope: EventEnvelope) -> None:
    logger.info(
        "Event consumed",
        extra={"event": "event_consumed", "event_id": str(envelope.event_id)},
    )


class EventConsumer:
    def __init__(
        self,
        *,
        name: str,
        handler: EventHandler = no_op_handler,
        transport: RedisEventStream | None = None,
        consumer_id: str | None = None,
        subscribed_topics: frozenset[str] | None = None,
    ) -> None:
        self.settings = get_settings()
        self.name = name
        self.group = f"forge-{name}"
        self.handler = handler
        self.transport = transport or RedisEventStream()
        self.consumer_id = consumer_id or f"{socket.gethostname()}:{os.getpid()}"
        self.subscribed_topics = subscribed_topics

    async def process_message(self, message_id: str, fields: dict[str, str]) -> bool:
        raw = json.loads(fields["envelope"])
        envelope = EventEnvelope.model_validate(raw)
        if self.subscribed_topics is not None and fields["topic"] not in self.subscribed_topics:
            await self.transport.acknowledge(self.group, message_id)
            return False

        last_error = ""
        for retry_index in range(self.settings.event_max_retries):
            async with get_session_factory()() as session:
                repo = EventConsumptionRepository(session)
                consumption, claimed = await repo.claim_attempt(
                    envelope.event_id,
                    self.name,
                    self.settings.event_processing_lease_seconds,
                )
                await session.commit()
                attempt = consumption.attempt
            if not claimed:
                await self.transport.acknowledge(self.group, message_id)
                return False
            try:
                if envelope.event_version != 1:
                    raise ValueError(f"Unsupported event envelope version {envelope.event_version}")
                await self.handler(envelope)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                async with get_session_factory()() as session:
                    repo = EventConsumptionRepository(session)
                    consumption = await repo.get_or_create(envelope.event_id, self.name)
                    await repo.mark_retrying(consumption, last_error)
                    await session.commit()
                logger.warning(
                    "Event consumer retry scheduled",
                    extra={
                        "event": "event_consumer_retry",
                        "event_id": str(envelope.event_id),
                        "consumer": self.name,
                        "attempt": attempt,
                    },
                )
                if retry_index + 1 < self.settings.event_max_retries:
                    await asyncio.sleep(self.settings.event_retry_base_seconds * (2**retry_index))
                continue

            async with get_session_factory()() as session:
                repo = EventConsumptionRepository(session)
                consumption = await repo.get_or_create(envelope.event_id, self.name)
                await repo.mark_succeeded(consumption)
                await EventWorkerHealthRepository(session).heartbeat(f"consumer:{self.name}")
                await session.commit()
            await self.transport.acknowledge(self.group, message_id)
            logger.info(
                "Event processing succeeded",
                extra={
                    "event": "event_processing_succeeded",
                    "event_id": str(envelope.event_id),
                    "consumer": self.name,
                    "attempt": attempt,
                },
            )
            return True

        dlq_message_id = await self.transport.dead_letter(
            consumer=self.name,
            source_message_id=message_id,
            envelope=raw,
            reason=last_error,
            attempts=attempt,
        )
        async with get_session_factory()() as session:
            repo = EventConsumptionRepository(session)
            consumption = await repo.get_or_create(envelope.event_id, self.name)
            await repo.mark_failed(consumption, last_error)
            await EventDeadLetterRepository(session).add_once(
                EventDeadLetter(
                    event_id=envelope.event_id,
                    consumer=self.name,
                    event_type=envelope.event_type,
                    original_event=raw,
                    failure_reason=last_error,
                    attempts=attempt,
                    redis_message_id=dlq_message_id,
                    failed_at=datetime.now(UTC),
                )
            )
            await EventWorkerHealthRepository(session).heartbeat(
                f"consumer:{self.name}", last_error
            )
            await session.commit()
        await self.transport.acknowledge(self.group, message_id)
        logger.error(
            "Event dead-lettered",
            extra={
                "event": "event_dead_lettered",
                "event_id": str(envelope.event_id),
                "consumer": self.name,
                "attempts": attempt,
                "redis_message_id": dlq_message_id,
            },
        )
        return False

    async def _process_rows(self, rows: list[tuple[str, dict[str, str]]]) -> None:
        for message_id, fields in rows:
            try:
                await self.process_message(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Event consumer message failed",
                    extra={
                        "event": "event_consumer_message_failed",
                        "consumer": self.name,
                        "redis_message_id": message_id,
                    },
                )

    async def run(self) -> None:
        await self.transport.ensure_group(self.group)
        logger.info(
            "Event consumer started",
            extra={"event": "event_consumer_started", "consumer": self.name},
        )
        while True:
            try:
                reclaimed = await self.transport.reclaim_stale(self.group, self.consumer_id, 25)
                await self._process_rows(reclaimed)
                streams = await self.transport.read_group(self.group, self.consumer_id, 25)
                for _, rows in streams:
                    await self._process_rows(rows)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Event consumer loop failed",
                    extra={"event": "event_consumer_loop_failed", "consumer": self.name},
                )
                await asyncio.sleep(1)


async def main() -> None:
    await asyncio.gather(
        EventConsumer(name="audit").run(),
        EventConsumer(name="dashboard").run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
