from httpx import AsyncClient
from sqlalchemy import select

from app.domain.models import Event, EventOutbox
from app.infrastructure.database import get_session_factory
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def test_mutations_persist_creation_and_status_events(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED", "Ready for execution")

    async with get_session_factory()() as session:
        events = list(await session.scalars(select(Event).order_by(Event.created_at, Event.type)))
        outbox = list(await session.scalars(select(EventOutbox)))

    event_types = {event.type for event in events}
    assert {
        "COMPANY_CREATED",
        "PROJECT_CREATED",
        "AGENT_CREATED",
        "TASK_CREATED",
        "TASK_STATUS_CHANGED",
    } <= event_types
    task_event = next(event for event in events if event.type == "TASK_CREATED")
    assert task_event.task_id is not None
    assert task_event.company_id is not None
    assert len(outbox) == len(events)
    assert all(row.payload["event_version"] == 1 for row in outbox)
