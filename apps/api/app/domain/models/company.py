from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import CompanyStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent import Agent
    from app.domain.models.event import Event
    from app.domain.models.mission import Mission
    from app.domain.models.project import Project
    from app.domain.models.task import Task


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("capital_available >= 0", name="company_capital_nonnegative"),
        CheckConstraint("revenue_recorded >= 0", name="company_revenue_nonnegative"),
        CheckConstraint("paid_ai_budget >= 0", name="company_ai_budget_nonnegative"),
        CheckConstraint("ai_spend_recorded >= 0", name="company_ai_spend_nonnegative"),
        Index("ix_companies_status", "status"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CompanyStatus] = mapped_column(
        Enum(CompanyStatus, name="company_status"),
        default=CompanyStatus.CREATED,
        server_default=CompanyStatus.CREATED.value,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text)
    capital_available: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    revenue_recorded: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    paid_ai_budget: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )
    ai_spend_recorded: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False
    )

    projects: Mapped[list[Project]] = relationship(back_populates="company")
    agents: Mapped[list[Agent]] = relationship(back_populates="company")
    tasks: Mapped[list[Task]] = relationship(back_populates="company")
    events: Mapped[list[Event]] = relationship(back_populates="company")
    missions: Mapped[list[Mission]] = relationship(back_populates="company")
