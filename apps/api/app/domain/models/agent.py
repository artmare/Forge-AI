from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import AgentStatus
from app.domain.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.domain.models.agent_run import AgentRun
    from app.domain.models.company import Company
    from app.domain.models.event import Event
    from app.domain.models.execution_job import ExecutionJob
    from app.domain.models.task import Task
    from app.domain.models.task_run import TaskRun
    from app.domain.models.tool_call import ToolCall


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_company_id_status", "company_id", "status"),
        Index("ix_agents_company_id_role", "company_id", "role"),
        Index("ix_agents_status", "status"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"),
        default=AgentStatus.CREATED,
        server_default=AgentStatus.CREATED.value,
        nullable=False,
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    permissions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="agents")
    assigned_tasks: Mapped[list[Task]] = relationship(back_populates="assigned_agent")
    task_runs: Mapped[list[TaskRun]] = relationship(back_populates="agent")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="agent")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="agent")
    events: Mapped[list[Event]] = relationship(back_populates="agent")
    execution_jobs: Mapped[list[ExecutionJob]] = relationship(back_populates="agent")
