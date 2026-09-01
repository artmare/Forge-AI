from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class RuntimeControl(TimestampMixin, Base):
    __tablename__ = "runtime_controls"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
