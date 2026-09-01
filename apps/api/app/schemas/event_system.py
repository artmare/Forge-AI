from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import EventConsumptionStatus, OutboxStatus


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str
    event_version: int = Field(default=1, gt=0)
    occurred_at: datetime
    source: str
    company_id: UUID | None = None
    project_id: UUID | None = None
    agent_id: UUID | None = None
    task_id: UUID | None = None
    correlation_id: UUID
    causation_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OutboxResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    replay_of_outbox_id: UUID | None
    topic: str
    status: OutboxStatus
    attempts: int
    available_at: datetime
    processing_started_at: datetime | None
    published_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class EventConsumptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    consumer: str
    status: EventConsumptionStatus
    attempt: int
    processing_started_at: datetime | None
    processed_at: datetime | None
    error: str | None


class DeadLetterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    consumer: str
    event_type: str
    original_event: dict[str, Any]
    failure_reason: str
    attempts: int
    redis_message_id: str | None
    failed_at: datetime


class EventStatsResponse(BaseModel):
    pending: int
    processing: int
    published: int
    failed: int
    dlq: int
    publisher_healthy: bool
    publisher_heartbeat_at: datetime | None
