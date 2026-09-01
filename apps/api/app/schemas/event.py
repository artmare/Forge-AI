from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import NonEmptyText, ORMResponse
from app.schemas.event_system import EventConsumptionResponse, OutboxResponse


class EventCreate(ORMResponse):
    company_id: UUID
    project_id: UUID | None = None
    agent_id: UUID | None = None
    task_id: UUID | None = None
    type: NonEmptyText
    message: NonEmptyText
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventUpdate(ORMResponse):
    message: NonEmptyText | None = None
    metadata: dict[str, Any] | None = None


class EventResponse(ORMResponse):
    id: UUID
    company_id: UUID | None
    project_id: UUID | None
    agent_id: UUID | None
    task_id: UUID | None
    type: str
    topic: str
    event_version: int
    source: str
    correlation_id: UUID
    causation_id: UUID | None
    message: str
    metadata: dict[str, Any] = Field(validation_alias="details", serialization_alias="metadata")
    created_at: datetime


class EventDetailResponse(EventResponse):
    publication: list[OutboxResponse]
    consumptions: list[EventConsumptionResponse]
