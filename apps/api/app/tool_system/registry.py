from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.tools import development_definitions
from app.tool_system.contracts import ToolDefinition, ToolDefinitionPublic
from app.tool_system.filesystem import FilesystemTools, filesystem_definitions
from app.tool_system.workspace import WorkspaceManager


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Tool '{definition.name}' is already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def list(self, *, enabled_only: bool = False) -> list[ToolDefinition]:
        definitions = sorted(self._definitions.values(), key=lambda item: item.name)
        if enabled_only:
            return [definition for definition in definitions if definition.enabled]
        return definitions

    def public(self) -> list[ToolDefinitionPublic]:
        return [definition.public() for definition in self.list()]

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, session: AsyncSession | None = None
    ) -> ToolRegistry:
        resolved = settings or get_settings()
        registry = cls()
        workspace = WorkspaceManager(resolved.tool_workspace_root)
        tools = FilesystemTools(
            workspace,
            read_max_bytes=resolved.tool_file_read_max_bytes,
            write_max_bytes=resolved.tool_file_write_max_bytes,
        )
        globally_enabled = resolved.tools_enabled
        for definition in filesystem_definitions(
            tools,
            timeout_seconds=resolved.filesystem_tool_timeout_seconds,
            list_enabled=globally_enabled and resolved.filesystem_list_enabled,
            read_enabled=globally_enabled and resolved.filesystem_read_enabled,
            write_enabled=globally_enabled and resolved.filesystem_write_enabled,
        ):
            registry.register(definition)
        if session is not None:
            for definition in development_definitions(session, resolved):
                registry.register(definition)
        return registry
