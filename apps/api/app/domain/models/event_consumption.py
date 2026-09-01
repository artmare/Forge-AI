from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import EventConsumptionStatus
from app.domain.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.event import Event


class EventConsumption(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "event_consumptions"
    __table_args__ = (
        CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
        UniqueConstraint("event_id", "consumer", name="uq_event_consumptions_event_consumer"),
        Index("ix_event_consumptions_consumer_status", "consumer", "status"),
    )

    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EventConsumptionStatus] = mapped_column(
        Enum(EventConsumptionStatus, name="event_consumption_status"),
        default=EventConsumptionStatus.PROCESSING,
        server_default=EventConsumptionStatus.PROCESSING.value,
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )

    event: Mapped[Event] = relationship(back_populates="consumptions")
