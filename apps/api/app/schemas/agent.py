from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.enums import AgentStatus, ToolPermission
from app.schemas.common import NonEmptyText, ORMResponse, PartialUpdate


def conservative_permissions() -> dict[ToolPermission, bool]:
    return {permission: False for permission in ToolPermission}


class AgentCreate(ORMResponse):
    name: NonEmptyText
    role: NonEmptyText
    configuration: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[ToolPermission, bool] = Field(default_factory=conservative_permissions)

    @model_validator(mode="after")
    def fill_permission_defaults(self) -> "AgentCreate":
        self.permissions = {**conservative_permissions(), **self.permissions}
        return self


class AgentUpdate(PartialUpdate):
    non_nullable_fields = frozenset({"name", "role", "status", "configuration", "permissions"})

    name: NonEmptyText | None = None
    role: NonEmptyText | None = None
    status: AgentStatus | None = None
    configuration: dict[str, Any] | None = None
    permissions: dict[ToolPermission, bool] | None = None

    @model_validator(mode="after")
    def fill_permission_defaults(self) -> "AgentUpdate":
        if self.permissions is not None:
            self.permissions = {**conservative_permissions(), **self.permissions}
        return self


class AgentResponse(ORMResponse):
    id: UUID
    company_id: UUID
    name: str
    role: str
    status: AgentStatus
    configuration: dict[str, Any]
    permissions: dict[str, Any]
    created_at: datetime
    updated_at: datetime
