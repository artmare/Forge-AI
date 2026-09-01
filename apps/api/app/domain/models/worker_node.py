from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import WorkerStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.execution_job import ExecutionJob


class WorkerNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "worker_nodes"
    __table_args__ = (
        CheckConstraint("concurrency > 0", name="concurrency_positive"),
        CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= started_at", name="stopped_after_start"
        ),
        Index("uq_worker_nodes_worker_key", "worker_key", unique=True),
        Index("ix_worker_nodes_status_heartbeat", "status", "last_heartbeat_at"),
    )

    worker_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus, name="worker_status"),
        default=WorkerStatus.ONLINE,
        server_default=WorkerStatus.ONLINE.value,
        nullable=False,
    )
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
        nullable=False,
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    execution_jobs: Mapped[list[ExecutionJob]] = relationship(back_populates="worker")
