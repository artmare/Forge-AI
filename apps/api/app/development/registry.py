from __future__ import annotations

from app.core.config import Settings, get_settings
from app.development.contracts import CommandDefinition
from app.domain.enums import DevelopmentAction, ToolPermission, ToolRiskLevel
from app.domain.enums import DevelopmentAction as A
from app.domain.enums import DevelopmentProjectType as P


class CommandRegistry:
    """Forge-owned action catalog; it never accepts executable names from a model."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        output = self.settings.development_output_max_bytes
        execute = ToolPermission.DEVELOPMENT_EXECUTE.value
        install = ToolPermission.DEVELOPMENT_INSTALL_DEPENDENCIES.value
        git_read = ToolPermission.GIT_READ.value
        git_write = ToolPermission.GIT_WRITE.value
        any_project = frozenset(P)
        self._definitions = {
            A.NODE_INSTALL: CommandDefinition(
                A.NODE_INSTALL,
                "Install package-lock dependencies with npm ci.",
                frozenset({P.NODE}),
                install,
                self.settings.development_install_timeout_seconds,
                True,
                output,
                ToolRiskLevel.HIGH,
            ),
            A.NODE_TEST: CommandDefinition(
                A.NODE_TEST,
                "Run the declared npm test script.",
                frozenset({P.NODE}),
                execute,
                self.settings.development_test_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
                accepts_target=True,
            ),
            A.NODE_BUILD: CommandDefinition(
                A.NODE_BUILD,
                "Run the declared npm build script.",
                frozenset({P.NODE, P.STATIC_WEB}),
                execute,
                self.settings.development_build_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
            ),
            A.NODE_LINT: CommandDefinition(
                A.NODE_LINT,
                "Run the declared npm lint script.",
                frozenset({P.NODE}),
                execute,
                self.settings.development_lint_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
            ),
            A.NODE_TYPECHECK: CommandDefinition(
                A.NODE_TYPECHECK,
                "Run the declared npm typecheck script.",
                frozenset({P.NODE}),
                execute,
                self.settings.development_lint_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
            ),
            A.PYTHON_INSTALL: CommandDefinition(
                A.PYTHON_INSTALL,
                "Install declared Python requirements.",
                frozenset({P.PYTHON}),
                install,
                self.settings.development_install_timeout_seconds,
                True,
                output,
                ToolRiskLevel.HIGH,
            ),
            A.PYTHON_TEST: CommandDefinition(
                A.PYTHON_TEST,
                "Run pytest with an optional relative target.",
                frozenset({P.PYTHON}),
                execute,
                self.settings.development_test_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
                accepts_target=True,
            ),
            A.PYTHON_LINT: CommandDefinition(
                A.PYTHON_LINT,
                "Run Ruff on the project or a relative target.",
                frozenset({P.PYTHON}),
                execute,
                self.settings.development_lint_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
                accepts_target=True,
            ),
            A.GIT_INIT: CommandDefinition(
                A.GIT_INIT,
                "Initialize a local repository.",
                any_project,
                git_write,
                self.settings.git_tool_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
            ),
            A.GIT_STATUS: CommandDefinition(
                A.GIT_STATUS,
                "Inspect local repository changes.",
                any_project,
                git_read,
                self.settings.git_tool_timeout_seconds,
                False,
                output,
                ToolRiskLevel.LOW,
            ),
            A.GIT_DIFF: CommandDefinition(
                A.GIT_DIFF,
                "Inspect the bounded local diff.",
                any_project,
                git_read,
                self.settings.git_tool_timeout_seconds,
                False,
                output,
                ToolRiskLevel.LOW,
            ),
            A.GIT_LOG: CommandDefinition(
                A.GIT_LOG,
                "Inspect recent local checkpoints.",
                any_project,
                git_read,
                self.settings.git_tool_timeout_seconds,
                False,
                output,
                ToolRiskLevel.LOW,
            ),
            A.GIT_CHECKPOINT: CommandDefinition(
                A.GIT_CHECKPOINT,
                "Create a local task checkpoint.",
                any_project,
                git_write,
                self.settings.git_tool_timeout_seconds,
                False,
                output,
                ToolRiskLevel.MEDIUM,
            ),
        }

    def get(self, action: DevelopmentAction | str) -> CommandDefinition | None:
        try:
            key = action if isinstance(action, DevelopmentAction) else DevelopmentAction(action)
        except ValueError:
            return None
        return self._definitions.get(key)

    def list(self, *, enabled_only: bool = False) -> list[CommandDefinition]:
        values = sorted(self._definitions.values(), key=lambda item: item.action.value)
        return [item for item in values if item.enabled] if enabled_only else values
