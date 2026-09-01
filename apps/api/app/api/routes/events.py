import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.enums import OutboxStatus
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import EventOutbox
from app.infrastructure.database import get_session
from app.infrastructure.event_stream import RedisEventStream
from app.repositories.event import EventRepository
from app.repositories.event_consumption import EventConsumptionRepository
from app.repositories.event_dead_letter import EventDeadLetterRepository
from app.repositories.event_outbox import EventOutboxRepository
from app.repositories.event_worker_health import EventWorkerHealthRepository
from app.schemas.event import EventDetailResponse, EventResponse
from app.schemas.event_system import (
    DeadLetterResponse,
    EventConsumptionResponse,
    EventStatsResponse,
    OutboxResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/events", tags=["events"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=list[EventResponse])
async def list_events(
    session: Session,
    company_id: UUID | None = None,
    project_id: UUID | None = None,
    agent_id: UUID | None = None,
    task_id: UUID | None = None,
    event_type: Annotated[str | None, Query(alias="type")] = None,
    topic: str | None = None,
    correlation_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[EventResponse]:
    events = await EventRepository(session).list(
        company_id=company_id,
        project_id=project_id,
        agent_id=agent_id,
        task_id=task_id,
        event_type=event_type,
        topic=topic,
        correlation_id=correlation_id,
        date_from=date_from,
        date_to=date_to,
        offset=offset,
        limit=limit,
    )
    return [EventResponse.model_validate(event) for event in events]


@router.get("/stats", response_model=EventStatsResponse)
async def event_stats(session: Session) -> EventStatsResponse:
    settings = get_settings()
    counts = await EventOutboxRepository(session).status_counts()
    dlq = await EventDeadLetterRepository(session).count()
    health = await EventWorkerHealthRepository(session).get("publisher")
    threshold = datetime.now(UTC) - timedelta(seconds=settings.event_publisher_healthy_seconds)
    heartbeat = health.heartbeat_at if health else None
    return EventStatsResponse(
        pending=counts.get(OutboxStatus.PENDING, 0),
        processing=counts.get(OutboxStatus.PROCESSING, 0),
        published=counts.get(OutboxStatus.PUBLISHED, 0),
        failed=counts.get(OutboxStatus.FAILED, 0),
        dlq=dlq,
        publisher_healthy=heartbeat is not None and heartbeat >= threshold,
        publisher_heartbeat_at=heartbeat,
    )


@router.get("/dlq", response_model=list[DeadLetterResponse])
async def list_dead_letters(
    session: Session,
    consumer: str | None = None,
    event_type: Annotated[str | None, Query(alias="type")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[DeadLetterResponse]:
    rows = await EventDeadLetterRepository(session).list(
        consumer=consumer, event_type=event_type, offset=offset, limit=limit
    )
    return [DeadLetterResponse.model_validate(row) for row in rows]


async def _event_stream(
    request: Request, last_id: str, transport: RedisEventStream
) -> AsyncIterator[str]:
    yield "retry: 2000\n\n"
    while not await request.is_disconnected():
        try:
            streams = await transport.read(last_id)
            if not streams:
                yield ": heartbeat\n\n"
                continue
            for _, messages in streams:
                for message_id, fields in messages:
                    last_id = message_id
                    envelope = json.loads(fields["envelope"])
                    data = {"stream_id": message_id, "topic": fields["topic"], **envelope}
                    yield f"id: {message_id}\nevent: forge-event\ndata: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "SSE event stream unavailable",
                extra={"event": "event_stream_unavailable", "error_type": type(exc).__name__},
            )
            yield 'event: transport-error\ndata: {"retrying":true}\n\n'
            await asyncio.sleep(2)


@router.get("/stream")
async def stream_events(
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request, last_event_id or "$", RedisEventStream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{event_id}", response_model=EventDetailResponse)
async def get_event(event_id: UUID, session: Session) -> EventDetailResponse:
    event = await EventRepository(session).get(event_id)
    if event is None:
        raise EntityNotFoundError("Event")
    publication = await EventOutboxRepository(session).list_for_event(event_id)
    consumptions = await EventConsumptionRepository(session).list_for_event(event_id)
    base = EventResponse.model_validate(event).model_dump()
    base["details"] = base.pop("metadata")
    return EventDetailResponse(
        **base,
        publication=[OutboxResponse.model_validate(row) for row in publication],
        consumptions=[EventConsumptionResponse.model_validate(row) for row in consumptions],
    )


@router.post(
    "/{event_id}/replay", response_model=OutboxResponse, status_code=status.HTTP_202_ACCEPTED
)
async def replay_event(event_id: UUID, session: Session) -> OutboxResponse:
    event = await EventRepository(session).get(event_id)
    if event is None:
        raise EntityNotFoundError("Event")
    outbox = EventOutboxRepository(session)
    original = await outbox.latest_for_event(event_id)
    if original is None:  # Defensive for legacy/corrupt rows.
        raise EntityNotFoundError("Event publication")
    replay = await outbox.add(
        EventOutbox(
            event_id=event_id,
            replay_of_outbox_id=original.id,
            topic=original.topic,
            payload=original.payload,
        )
    )
    await session.commit()
    return OutboxResponse.model_validate(replay)
