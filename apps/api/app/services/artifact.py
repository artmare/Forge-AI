from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import ToolCallStatus
from app.domain.exceptions import DomainError, EntityNotFoundError
from app.domain.models import Agent, Task, ToolCall
from app.repositories.project import ProjectRepository
from app.schemas.artifact import ArtifactContentResponse, ArtifactMetadataResponse
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager


@dataclass(frozen=True)
class PreviewType:
    label: str
    kind: str


PREVIEW_TYPES = {
    ".md": PreviewType("Markdown", "markdown"),
    ".txt": PreviewType("Text", "text"),
    ".json": PreviewType("JSON", "json"),
    ".yaml": PreviewType("YAML", "code"),
    ".yml": PreviewType("YAML", "code"),
    ".csv": PreviewType("CSV", "code"),
    ".py": PreviewType("Python", "code"),
    ".ts": PreviewType("TypeScript", "code"),
    ".tsx": PreviewType("TypeScript JSX", "code"),
    ".js": PreviewType("JavaScript", "code"),
    ".jsx": PreviewType("JavaScript JSX", "code"),
    ".css": PreviewType("CSS", "code"),
    ".html": PreviewType("HTML source", "code"),
}


class ArtifactService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.projects = ProjectRepository(session)
        self.workspace = WorkspaceManager(self.settings.tool_workspace_root)

    async def list(self, project_id: UUID) -> list[ArtifactMetadataResponse]:
        project = await self.projects.get(project_id)
        if project is None:
            raise EntityNotFoundError("Project")
        ownership = await self._ownership(project_id)
        return await asyncio.to_thread(self._list_sync, project.company_id, project.id, ownership)

    async def read(self, project_id: UUID, requested_path: str) -> ArtifactContentResponse:
        project = await self.projects.get(project_id)
        if project is None:
            raise EntityNotFoundError("Project")
        ownership = await self._ownership(project_id)
        return await asyncio.to_thread(
            self._read_sync,
            project.company_id,
            project.id,
            requested_path,
            ownership,
        )

    async def _ownership(self, project_id: UUID) -> dict[str, dict[str, object]]:
        rows = await self.session.execute(
            select(ToolCall, Task.title, Agent.name)
            .join(Task, Task.id == ToolCall.task_id)
            .join(Agent, Agent.id == ToolCall.agent_id)
            .where(
                Task.project_id == project_id,
                ToolCall.tool_name == "filesystem.write",
                ToolCall.status == ToolCallStatus.SUCCEEDED,
            )
            .order_by(ToolCall.completed_at.desc(), ToolCall.id.desc())
        )
        ownership: dict[str, dict[str, object]] = {}
        for call, task_title, agent_name in rows:
            path = call.result.get("path") if call.result else None
            if not isinstance(path, str) or path in ownership:
                continue
            ownership[path] = {
                "task_id": call.task_id,
                "task_title": task_title,
                "agent_id": call.agent_id,
                "agent_name": agent_name,
                "tool_call_id": call.id,
            }
        return ownership

    def _list_sync(
        self,
        company_id: UUID,
        project_id: UUID,
        ownership: dict[str, dict[str, object]],
    ) -> list[ArtifactMetadataResponse]:
        workspace = self.workspace.project_workspace(company_id, project_id)
        artifacts: list[ArtifactMetadataResponse] = []
        for root, directories, filenames in os.walk(workspace, followlinks=False):
            root_path = Path(root)
            directories[:] = [
                name
                for name in sorted(directories)
                if not self._is_hidden(name) and not (root_path / name).is_symlink()
            ]
            for filename in sorted(filenames):
                path = root_path / filename
                if self._is_hidden(filename) or path.is_symlink():
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                relative = self.workspace.relative(workspace, path)
                artifacts.append(self._metadata(path, relative, info, ownership))
        return sorted(artifacts, key=lambda artifact: artifact.path.casefold())

    def _read_sync(
        self,
        company_id: UUID,
        project_id: UUID,
        requested_path: str,
        ownership: dict[str, dict[str, object]],
    ) -> ArtifactContentResponse:
        self._reject_hidden_path(requested_path)
        try:
            workspace, path = self.workspace.resolve(
                company_id, project_id, requested_path, must_exist=True
            )
            self.workspace.ensure_regular_file(path)
        except ToolSystemError as exc:
            raise self._access_error(exc) from exc
        relative = self.workspace.relative(workspace, path)
        preview = PREVIEW_TYPES.get(path.suffix.casefold())
        if preview is None:
            raise DomainError(
                415,
                "ARTIFACT_UNSUPPORTED_TYPE",
                "Preview is not available for this file type.",
            )
        info = path.stat()
        if info.st_size > self.settings.tool_file_read_max_bytes:
            raise DomainError(
                413,
                "ARTIFACT_TOO_LARGE",
                "This file is too large to preview.",
            )
        content = path.read_bytes()
        if len(content) > self.settings.tool_file_read_max_bytes:
            raise DomainError(
                413,
                "ARTIFACT_TOO_LARGE",
                "This file is too large to preview.",
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError(
                415,
                "ARTIFACT_NOT_UTF8",
                "Preview is not available because this file is not UTF-8 text.",
            ) from exc
        metadata = self._metadata(path, relative, info, ownership)
        return ArtifactContentResponse(**metadata.model_dump(), content=decoded)

    @staticmethod
    def _metadata(
        path: Path,
        relative: str,
        info: os.stat_result,
        ownership: dict[str, dict[str, object]],
    ) -> ArtifactMetadataResponse:
        preview = PREVIEW_TYPES.get(path.suffix.casefold())
        return ArtifactMetadataResponse(
            name=path.name,
            path=relative,
            size_bytes=info.st_size,
            modified_at=datetime.fromtimestamp(info.st_mtime, UTC),
            file_type=preview.label if preview else "Unsupported file",
            preview_kind=preview.kind if preview else None,
            previewable=preview is not None,
            **ownership.get(relative, {}),
        )

    @staticmethod
    def _reject_hidden_path(requested_path: str) -> None:
        if not requested_path or "\x00" in requested_path:
            return
        posix = PurePosixPath(requested_path.replace("\\", "/"))
        windows = PureWindowsPath(requested_path)
        if any(ArtifactService._is_hidden(part) for part in (*posix.parts, *windows.parts)):
            raise DomainError(
                403,
                "ARTIFACT_SENSITIVE_PATH",
                "This project path is not available for preview.",
            )

    @staticmethod
    def _is_hidden(name: str) -> bool:
        return name not in {".", ".."} and name.startswith(".")

    @staticmethod
    def _access_error(error: ToolSystemError) -> DomainError:
        if error.code == "FILE_NOT_FOUND":
            return DomainError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
        if error.code == "UNSUPPORTED_FILE_TYPE":
            return DomainError(400, "ARTIFACT_NOT_FILE", "Artifact path is not a file.")
        return DomainError(
            400,
            "ARTIFACT_PATH_REJECTED",
            "Artifact path is not a valid project-relative file path.",
        )
