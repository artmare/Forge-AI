import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select

from app.api.routes.events import _event_stream
from app.core.config import get_settings
from app.domain.enums import EventConsumptionStatus, OutboxStatus
from app.domain.models import Event, EventConsumption, EventDeadLetter, EventOutbox
from app.infrastructure.database import get_session_factory
from app.infrastructure.event_stream import RedisEventStream
from app.repositories.event_outbox import EventOutboxRepository
from app.services.event_factory import EventFactory
from app.workers.event_consumer import EventConsumer
from app.workers.event_publisher import EventPublisher
from tests.helpers import create_company, create_task


class FailingPublisherTransport:
    async def publish(self, topic: str, envelope: dict[str, object]) -> str:
        del topic, envelope
        raise ConnectionError("Redis unavailable")


async def _create_system_event(message: str = "test") -> UUID:
    async with get_session_factory()() as session:
        event = await EventFactory(session).create(event_type="SYSTEM_TEST", message=message)
        await session.commit()
        return event.id


async def test_event_and_outbox_commit_atomically_and_rollback_together() -> None:
    async with get_session_factory()() as session:
        await EventFactory(session).create(event_type="SYSTEM_ROLLBACK", message="rollback")
        await session.rollback()

    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == 0

    await _create_system_event()
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == 1


async def test_concurrent_publishers_claim_disjoint_batches() -> None:
    for index in range(10):
        await _create_system_event(str(index))

    async def claim(worker: str) -> set[UUID]:
        async with get_session_factory()() as session:
            rows = await EventOutboxRepository(session).claim_batch(
                worker_id=worker, batch_size=5, lease_seconds=30
            )
            await asyncio.sleep(0.05)
            await session.commit()
            return {row.id for row in rows}

    first, second = await asyncio.gather(claim("one"), claim("two"))
    assert len(first) == len(second) == 5
    assert first.isdisjoint(second)


async def test_stale_processing_claim_is_recovered() -> None:
    await _create_system_event()
    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox))
        assert row is not None
        row.status = OutboxStatus.PROCESSING
        row.processing_started_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

    async with get_session_factory()() as session:
        claimed = await EventOutboxRepository(session).claim_batch(
            worker_id="recovery", batch_size=1, lease_seconds=30
        )
        assert [row.worker_id for row in claimed] == ["recovery"]


async def test_publisher_records_outage_then_recovers_to_redis() -> None:
    event_id = await _create_system_event()
    failed_publisher = EventPublisher(transport=FailingPublisherTransport(), worker_id="failing")  # type: ignore[arg-type]
    assert await failed_publisher.process_batch() == 1

    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        assert row.status == OutboxStatus.FAILED
        assert "Redis unavailable" in (row.last_error or "")
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await EventPublisher(worker_id="recovered").process_batch() == 1
    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        assert row.status == OutboxStatus.PUBLISHED


async def test_consumers_are_idempotent_per_event_and_consumer() -> None:
    event_id = await _create_system_event()
    await EventPublisher(worker_id="publisher").process_batch()
    transport = RedisEventStream()
    calls = 0

    async def handler(_envelope) -> None:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)

    consumer = EventConsumer(name="audit", handler=handler, transport=transport)
    await transport.ensure_group(consumer.group)
    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        fields = {
            "event_id": str(event_id),
            "event_type": row.payload["event_type"],
            "topic": row.topic,
            "envelope": json.dumps(row.payload),
        }
    results = await asyncio.gather(
        consumer.process_message("1-0", fields),
        consumer.process_message("3-0", fields),
    )
    assert calls == 1
    assert sorted(results) == [False, True]
    async with get_session_factory()() as session:
        records = list(await session.scalars(select(EventConsumption)))
        assert len(records) == 1
        assert records[0].status == EventConsumptionStatus.SUCCEEDED


async def test_consumer_bounded_retry_writes_dead_letter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    event_id = await _create_system_event()
    await EventPublisher(worker_id="publisher").process_batch()
    settings = get_settings()
    monkeypatch.setattr(settings, "event_max_retries", 2)
    monkeypatch.setattr(settings, "event_retry_base_seconds", 0)

    async def fail(_envelope) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("consumer failed")

    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        fields = {
            "event_id": str(event_id),
            "event_type": row.payload["event_type"],
            "topic": row.topic,
            "envelope": json.dumps(row.payload),
        }
    consumer = EventConsumer(name="broken", handler=fail)
    await consumer.transport.ensure_group(consumer.group)
    await consumer.process_message("2-0", fields)
    async with get_session_factory()() as session:
        consumption = await session.scalar(select(EventConsumption))
        dead_letter = await session.scalar(select(EventDeadLetter))
        assert consumption is not None and consumption.attempt == 2
        assert consumption.status == EventConsumptionStatus.FAILED
        assert dead_letter is not None and dead_letter.attempts == 2


async def test_consumer_transient_retry_eventually_succeeds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    event_id = await _create_system_event()
    await EventPublisher(worker_id="publisher").process_batch()
    monkeypatch.setattr(get_settings(), "event_retry_base_seconds", 0)
    calls = 0

    async def transient(_envelope) -> None:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("try again")

    async with get_session_factory()() as session:
        row = await session.scalar(select(EventOutbox).where(EventOutbox.event_id == event_id))
        assert row is not None
        fields = {
            "event_id": str(event_id),
            "event_type": row.payload["event_type"],
            "topic": row.topic,
            "envelope": json.dumps(row.payload),
        }
    consumer = EventConsumer(name="transient", handler=transient)
    await consumer.transport.ensure_group(consumer.group)
    assert await consumer.process_message("4-0", fields) is True
    async with get_session_factory()() as session:
        consumption = await session.scalar(select(EventConsumption))
        assert consumption is not None and consumption.attempt == 2
        assert consumption.status == EventConsumptionStatus.SUCCEEDED


async def test_event_apis_filter_detail_stats_and_replay(client: AsyncClient) -> None:
    company = await create_company(client, "events-api")
    await create_task(client, company["id"], None, None)
    await EventPublisher(worker_id="api-test").process_batch()

    listing = await client.get(f"/api/v1/events?company_id={company['id']}&type=TASK_CREATED")
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    event_id = listing.json()[0]["id"]
    detail = await client.get(f"/api/v1/events/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["publication"][0]["status"] == "PUBLISHED"
    stats = await client.get("/api/v1/events/stats")
    assert stats.status_code == 200
    assert stats.json()["published"] == 2
    replay = await client.post(f"/api/v1/events/{event_id}/replay")
    assert replay.status_code == 202
    assert replay.json()["replay_of_outbox_id"] is not None


async def test_sse_generator_delivers_new_event() -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    generator = _event_stream(ConnectedRequest(), "$", RedisEventStream())  # type: ignore[arg-type]
    assert await anext(generator) == "retry: 2000\n\n"
    pending = asyncio.create_task(anext(generator))
    await asyncio.sleep(0.05)
    event_id = await _create_system_event("live")
    await EventPublisher(worker_id="sse").process_batch()
    message = await asyncio.wait_for(pending, timeout=3)
    assert "event: forge-event" in message
    assert str(event_id) in message
    stream_id = message.split("id: ", 1)[1].split("\n", 1)[0]
    await generator.aclose()

    reconnected = _event_stream(ConnectedRequest(), stream_id, RedisEventStream())  # type: ignore[arg-type]
    assert await anext(reconnected) == "retry: 2000\n\n"
    pending = asyncio.create_task(anext(reconnected))
    await asyncio.sleep(0.05)
    second_event_id = await _create_system_event("after reconnect")
    await EventPublisher(worker_id="sse-reconnect").process_batch()
    message = await asyncio.wait_for(pending, timeout=3)
    assert str(second_event_id) in message
    await reconnected.aclose()
