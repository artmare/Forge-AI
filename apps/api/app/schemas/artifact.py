from datetime import datetime
from uuid import UUID

from app.schemas.common import ORMResponse


class ArtifactMetadataResponse(ORMResponse):
    name: str
    path: str
    size_bytes: int
    modified_at: datetime
    file_type: str
    preview_kind: str | None
    previewable: bool
    task_id: UUID | None = None
    task_title: str | None = None
    agent_id: UUID | None = None
    agent_name: str | None = None
    tool_call_id: UUID | None = None


class ArtifactContentResponse(ArtifactMetadataResponse):
    content: str
