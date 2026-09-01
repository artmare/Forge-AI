from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.models import Event, EventOutbox
from app.repositories.event import EventRepository
from app.repositories.event_outbox import EventOutboxRepository
from app.schemas.event_system import EventEnvelope

EVENT_TOPICS: tuple[tuple[str, str], ...] = (
    ("MISSION_", "forge.mission"),
    ("PLANNING_RUN_", "forge.planning"),
    ("COMPANY_", "forge.company"),
    ("PROJECT_", "forge.project"),
    ("AGENT_", "forge.agent"),
    ("EXECUTION_JOB_", "forge.execution"),
    ("WORKER_", "forge.worker"),
    ("ORCHESTRATOR_", "forge.orchestrator"),
    ("TOOL_", "forge.tool"),
    ("TASK_RUN_", "forge.task_run"),
    ("TASK_", "forge.task"),
)


def topic_for_event_type(event_type: str) -> str:
    return next(
        (topic for prefix, topic in EVENT_TOPICS if event_type.startswith(prefix)),
        "forge.system",
    )


class EventFactory:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.events = EventRepository(session)
        self.outbox = EventOutboxRepository(session)

    async def create(
        self,
        *,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        topic: str | None = None,
        company_id: UUID | None = None,
        project_id: UUID | None = None,
        agent_id: UUID | None = None,
        task_id: UUID | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        event_version: int = 1,
    ) -> Event:
        event_id = uuid4()
        occurred_at = datetime.now(UTC)
        resolved_topic = topic or topic_for_event_type(event_type)
        resolved_correlation_id = (
            correlation_id or task_id or project_id or agent_id or company_id or event_id
        )
        details = payload or {}
        source = get_settings().event_source
        event = Event(
            id=event_id,
            company_id=company_id,
            project_id=project_id,
            agent_id=agent_id,
            task_id=task_id,
            type=event_type,
            topic=resolved_topic,
            event_version=event_version,
            source=source,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            message=message,
            details=details,
            created_at=occurred_at,
        )
        await self.events.add(event)
        envelope = EventEnvelope(
            event_id=event.id,
            event_type=event.type,
            event_version=event.event_version,
            occurred_at=event.created_at,
            source=event.source,
            company_id=event.company_id,
            project_id=event.project_id,
            agent_id=event.agent_id,
            task_id=event.task_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            payload={"message": message, **details},
        )
        await self.outbox.add(
            EventOutbox(
                event_id=event.id,
                topic=event.topic,
                payload=envelope.model_dump(mode="json"),
            )
        )
        return event
