from datetime import datetime
from uuid import UUID

from app.domain.enums import ProjectStatus
from app.schemas.common import NonEmptyText, ORMResponse, PartialUpdate


class ProjectCreate(ORMResponse):
    name: NonEmptyText
    description: str | None = None
    goal: NonEmptyText


class ProjectUpdate(PartialUpdate):
    non_nullable_fields = frozenset({"name", "goal", "status"})

    name: NonEmptyText | None = None
    description: str | None = None
    goal: NonEmptyText | None = None
    status: ProjectStatus | None = None


class ProjectResponse(ORMResponse):
    id: UUID
    company_id: UUID
    name: str
    description: str | None
    goal: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
