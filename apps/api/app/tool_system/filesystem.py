import asyncio
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ToolRiskLevel
from app.tool_system.contracts import ToolDefinition, ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager


class FilesystemPathInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default=".",
        min_length=1,
        max_length=4096,
        description=(
            "Project-relative path. Use '.' only with filesystem.list. "
            "filesystem.read requires an existing regular UTF-8 file."
        ),
    )


class FilesystemWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "Project-relative regular-file path to create or replace. The file need not exist; "
            "missing parent directories are created safely. Do not pass '.' or a directory path."
        ),
    )
    content: str = Field(description="Complete UTF-8 text content to write atomically.")


class FilesystemEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory", "symlink", "other"]
    size: int | None = None


class FilesystemListOutput(BaseModel):
    path: str
    entries: list[FilesystemEntry]


class FilesystemReadOutput(BaseModel):
    path: str
    content: str
    byte_size: int


class FilesystemWriteOutput(BaseModel):
    path: str
    byte_size: int
    created: bool
    profile_refresh: dict[str, object] | None = None


class FilesystemTools:
    def __init__(
        self,
        workspace: WorkspaceManager,
        *,
        read_max_bytes: int,
        write_max_bytes: int,
    ) -> None:
        self.workspace = workspace
        self.read_max_bytes = read_max_bytes
        self.write_max_bytes = write_max_bytes

    async def list_path(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> FilesystemListOutput:
        parsed = FilesystemPathInput.model_validate(arguments)
        return await asyncio.to_thread(self._list_path, parsed, context)

    def _list_path(
        self, arguments: FilesystemPathInput, context: ToolExecutionContext
    ) -> FilesystemListOutput:
        if context.project_id is None:
            raise ToolSystemError("WORKSPACE_NOT_AVAILABLE", "Task has no project workspace")
        workspace, path = self.workspace.resolve(
            context.company_id, context.project_id, arguments.path, must_exist=True
        )
        if not path.is_dir():
            raise ToolSystemError("NOT_A_DIRECTORY", "Requested path is not a directory")
        entries: list[FilesystemEntry] = []
        for child in sorted(path.iterdir(), key=lambda value: value.name):
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                kind: Literal["file", "directory", "symlink", "other"] = "symlink"
                size = None
            elif stat.S_ISREG(mode):
                kind = "file"
                size = child.stat().st_size
            elif stat.S_ISDIR(mode):
                kind = "directory"
                size = None
            else:
                kind = "other"
                size = None
            entries.append(
                FilesystemEntry(
                    name=child.name,
                    path=self.workspace.relative(workspace, child),
                    type=kind,
                    size=size,
                )
            )
        return FilesystemListOutput(path=self.workspace.relative(workspace, path), entries=entries)

    async def read_file(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> FilesystemReadOutput:
        parsed = FilesystemPathInput.model_validate(arguments)
        return await asyncio.to_thread(self._read_file, parsed, context)

    def _read_file(
        self, arguments: FilesystemPathInput, context: ToolExecutionContext
    ) -> FilesystemReadOutput:
        if context.project_id is None:
            raise ToolSystemError("WORKSPACE_NOT_AVAILABLE", "Task has no project workspace")
        workspace, path = self.workspace.resolve(
            context.company_id, context.project_id, arguments.path, must_exist=True
        )
        self.workspace.ensure_regular_file(path)
        size = path.stat().st_size
        if size > self.read_max_bytes:
            raise ToolSystemError("FILE_TOO_LARGE", "Requested file exceeds the read limit")
        content = path.read_bytes()
        if len(content) > self.read_max_bytes:
            raise ToolSystemError("FILE_TOO_LARGE", "Requested file exceeds the read limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolSystemError("FILE_NOT_TEXT", "Requested file is not UTF-8 text") from exc
        return FilesystemReadOutput(
            path=self.workspace.relative(workspace, path), content=text, byte_size=len(content)
        )

    async def write_file(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> FilesystemWriteOutput:
        parsed = FilesystemWriteInput.model_validate(arguments)
        return await asyncio.to_thread(self._write_file, parsed, context)

    def _write_file(
        self, arguments: FilesystemWriteInput, context: ToolExecutionContext
    ) -> FilesystemWriteOutput:
        if context.project_id is None:
            raise ToolSystemError("WORKSPACE_NOT_AVAILABLE", "Task has no project workspace")
        content = arguments.content.encode("utf-8")
        if len(content) > self.write_max_bytes:
            raise ToolSystemError("FILE_TOO_LARGE", "File content exceeds the write limit")
        workspace, path = self.workspace.resolve(
            context.company_id, context.project_id, arguments.path, must_exist=False
        )
        created = not path.exists()
        self.workspace.atomic_write(path, content)
        return FilesystemWriteOutput(
            path=self.workspace.relative(workspace, path),
            byte_size=len(content),
            created=created,
        )


def filesystem_definitions(
    tools: FilesystemTools,
    *,
    timeout_seconds: float,
    list_enabled: bool,
    read_enabled: bool,
    write_enabled: bool,
) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="filesystem.list",
            description=(
                "List safe metadata for a project-relative directory. Use path='.' for the "
                "workspace root; this tool does not read file contents or create files."
            ),
            input_model=FilesystemPathInput,
            output_model=FilesystemListOutput,
            risk_level=ToolRiskLevel.LOW,
            permission_required="filesystem.list",
            timeout_seconds=timeout_seconds,
            enabled=list_enabled,
            handler=tools.list_path,
        ),
        ToolDefinition(
            name="filesystem.read",
            description=(
                "Read one existing regular UTF-8 text file by project-relative path. Do not pass "
                "'.' or another directory; use filesystem.list for directories."
            ),
            input_model=FilesystemPathInput,
            output_model=FilesystemReadOutput,
            risk_level=ToolRiskLevel.LOW,
            permission_required="filesystem.read",
            timeout_seconds=timeout_seconds,
            enabled=read_enabled,
            handler=tools.read_file,
        ),
        ToolDefinition(
            name="filesystem.write",
            description=(
                "Create or replace one bounded UTF-8 text file by project-relative path. The "
                "target need not exist and missing parent directories are created safely."
            ),
            input_model=FilesystemWriteInput,
            output_model=FilesystemWriteOutput,
            risk_level=ToolRiskLevel.MEDIUM,
            permission_required="filesystem.write",
            timeout_seconds=timeout_seconds,
            enabled=write_enabled,
            handler=tools.write_file,
        ),
    ]
