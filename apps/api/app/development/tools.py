from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.service import DevelopmentExecutionService
from app.domain.enums import (
    DevelopmentAction,
    DevelopmentExecutionStatus,
    ToolPermission,
    ToolRiskLevel,
)
from app.tool_system.contracts import ToolDefinition, ToolExecutionContext


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


ExecutableAction = Literal[
    DevelopmentAction.NODE_TEST,
    DevelopmentAction.NODE_BUILD,
    DevelopmentAction.NODE_LINT,
    DevelopmentAction.NODE_TYPECHECK,
    DevelopmentAction.PYTHON_TEST,
    DevelopmentAction.PYTHON_LINT,
]


class ExecuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ExecutableAction
    target: str | None = Field(default=None, max_length=512)


class DevelopmentOutput(BaseModel):
    execution_id: str
    action: DevelopmentAction
    status: DevelopmentExecutionStatus
    exit_code: int | None
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    output_truncated: bool
    duration_ms: float | None
    change_summary: dict[str, Any] | None


class DevelopmentTools:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.service = DevelopmentExecutionService(session, settings=settings)

    async def execute(
        self, payload: ExecuteInput, context: ToolExecutionContext
    ) -> DevelopmentOutput:
        arguments = {"target": payload.target} if payload.target is not None else {}
        return self.output(
            await self.service.execute(DevelopmentAction(payload.action), arguments, context)
        )

    async def install(
        self, _payload: EmptyInput, context: ToolExecutionContext
    ) -> DevelopmentOutput:
        profile = await self.service.profiles.detect(context.project_id, commit=True)  # type: ignore[arg-type]
        if profile.install_action is None:
            from app.tool_system.errors import ToolSystemError

            raise ToolSystemError(
                "DEPENDENCY_INSTALL_UNAVAILABLE", "No reproducible install action is configured"
            )
        return self.output(await self.service.execute(profile.install_action, {}, context))

    async def git(
        self, action: DevelopmentAction, context: ToolExecutionContext
    ) -> DevelopmentOutput:
        return self.output(await self.service.execute(action, {}, context))

    @staticmethod
    def output(execution: Any) -> DevelopmentOutput:
        return DevelopmentOutput(
            execution_id=str(execution.id),
            action=execution.action,
            status=execution.status,
            exit_code=execution.exit_code,
            stdout_excerpt=execution.stdout_excerpt,
            stderr_excerpt=execution.stderr_excerpt,
            output_truncated=execution.output_truncated,
            duration_ms=float(execution.duration_ms) if execution.duration_ms is not None else None,
            change_summary=execution.change_summary,
        )


def development_definitions(
    session: AsyncSession, settings: Settings | None = None
) -> list[ToolDefinition]:
    resolved = settings or get_settings()
    tools = DevelopmentTools(session, resolved)

    def git_handler(action: DevelopmentAction):  # type: ignore[no-untyped-def]
        async def handler(_payload: EmptyInput, context: ToolExecutionContext) -> DevelopmentOutput:
            return await tools.git(action, context)

        return handler

    definitions = [
        ToolDefinition(
            "development.execute",
            "Run one allowlisted build, test, lint, or typecheck action. "
            "No command text is accepted.",
            ExecuteInput,
            DevelopmentOutput,
            ToolRiskLevel.MEDIUM,
            ToolPermission.DEVELOPMENT_EXECUTE.value,
            resolved.development_test_timeout_seconds + 20,
            resolved.development_enabled,
            tools.execute,
        ),
        ToolDefinition(
            "development.install_dependencies",
            "Install only dependencies already declared in a supported manifest/lockfile.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.HIGH,
            ToolPermission.DEVELOPMENT_INSTALL_DEPENDENCIES.value,
            resolved.development_install_timeout_seconds + 20,
            resolved.development_enabled,
            tools.install,
        ),
        ToolDefinition(
            "git.init",
            "Initialize the project workspace as a local Git repository.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.MEDIUM,
            ToolPermission.GIT_WRITE.value,
            resolved.git_tool_timeout_seconds + 20,
            resolved.development_enabled,
            git_handler(DevelopmentAction.GIT_INIT),
        ),
        ToolDefinition(
            "git.status",
            "Inspect bounded local workspace status.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.LOW,
            ToolPermission.GIT_READ.value,
            resolved.git_tool_timeout_seconds + 20,
            resolved.development_enabled,
            git_handler(DevelopmentAction.GIT_STATUS),
        ),
        ToolDefinition(
            "git.diff",
            "Inspect a bounded local source diff.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.LOW,
            ToolPermission.GIT_READ.value,
            resolved.git_tool_timeout_seconds + 20,
            resolved.development_enabled,
            git_handler(DevelopmentAction.GIT_DIFF),
        ),
        ToolDefinition(
            "git.log",
            "Inspect recent Forge-created local commits.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.LOW,
            ToolPermission.GIT_READ.value,
            resolved.git_tool_timeout_seconds + 20,
            resolved.development_enabled,
            git_handler(DevelopmentAction.GIT_LOG),
        ),
        ToolDefinition(
            "git.commit",
            "Create a local checkpoint with a Forge-owned task message; "
            "no remote operation exists.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.MEDIUM,
            ToolPermission.GIT_WRITE.value,
            resolved.git_tool_timeout_seconds + 20,
            resolved.development_enabled,
            git_handler(DevelopmentAction.GIT_CHECKPOINT),
        ),
    ]
    return definitions
