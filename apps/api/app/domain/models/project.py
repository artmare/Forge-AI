from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import ProjectStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.company import Company
    from app.domain.models.development import ProjectDevelopmentLease, ProjectDevelopmentProfile
    from app.domain.models.event import Event
    from app.domain.models.mission import Mission
    from app.domain.models.task import Task


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_company_id_status", "company_id", "status"),
        Index("ix_projects_status", "status"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status"),
        default=ProjectStatus.CREATED,
        server_default=ProjectStatus.CREATED.value,
        nullable=False,
    )

    company: Mapped[Company] = relationship(back_populates="projects")
    tasks: Mapped[list[Task]] = relationship(back_populates="project")
    events: Mapped[list[Event]] = relationship(back_populates="project")
    mission: Mapped[Mission | None] = relationship(back_populates="project", uselist=False)
    development_profile: Mapped[ProjectDevelopmentProfile | None] = relationship(
        back_populates="project", uselist=False
    )
    development_lease: Mapped[ProjectDevelopmentLease | None] = relationship(
        back_populates="project", uselist=False
    )
