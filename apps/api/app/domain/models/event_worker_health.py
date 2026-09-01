from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, UUIDPrimaryKeyMixin


class EventWorkerHealth(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "event_worker_health"
    __table_args__ = (
        UniqueConstraint("worker", name="uq_event_worker_health_worker"),
        Index("ix_event_worker_health_heartbeat_at", "heartbeat_at"),
    )

    worker: Mapped[str] = mapped_column(Text, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
