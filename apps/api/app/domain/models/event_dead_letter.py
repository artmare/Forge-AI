from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.event import Event


class EventDeadLetter(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "event_dead_letters"
    __table_args__ = (
        CheckConstraint("attempts > 0", name="attempts_positive"),
        UniqueConstraint("event_id", "consumer", name="uq_event_dead_letters_event_consumer"),
        Index("ix_event_dead_letters_consumer_failed_at", "consumer", "failed_at"),
        Index("ix_event_dead_letters_event_type", "event_type"),
    )

    event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    consumer: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    original_event: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    redis_message_id: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )

    event: Mapped[Event] = relationship(back_populates="dead_letters")
