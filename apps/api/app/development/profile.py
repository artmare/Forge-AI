from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import (
    DevelopmentAction,
    DevelopmentProjectType,
    PackageManager,
    TaskKind,
    TaskStatus,
)
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import Project, ProjectDevelopmentProfile, Task
from app.tool_system.workspace import WorkspaceManager


class DevelopmentProfileService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.workspace = WorkspaceManager(self.settings.tool_workspace_root)

    async def get(self, project_id: UUID) -> ProjectDevelopmentProfile | None:
        return await self.session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == project_id
            )
        )

    async def detect(
        self, project_id: UUID, *, commit: bool = True, create_workspace: bool = True
    ) -> ProjectDevelopmentProfile:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise EntityNotFoundError("Project")
        workspace = (
            self.workspace.project_workspace(project.company_id, project.id)
            if create_workspace
            else self.workspace.existing_project_workspace(project.company_id, project.id)
        )
        if workspace is None:
            workspace = self.workspace.configured_root / str(project.company_id) / str(project.id)
        detected = self._detect_files(workspace)
        profile = await self.get(project.id)
        if profile is None:
            profile = ProjectDevelopmentProfile(project_id=project.id, **detected)
            self.session.add(profile)
        else:
            for key, value in detected.items():
                setattr(profile, key, value)
        profile.last_refreshed_at = datetime.now(UTC)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return profile

    async def reconcile_active(self, limit: int = 100) -> int:
        project_ids = list(
            await self.session.scalars(
                select(Task.project_id)
                .where(
                    Task.kind == TaskKind.DEVELOPMENT,
                    Task.project_id.is_not(None),
                    Task.status.in_(
                        (
                            TaskStatus.CREATED,
                            TaskStatus.QUEUED,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.FIX_REQUIRED,
                            TaskStatus.REVIEW,
                        )
                    ),
                )
                .distinct()
                .limit(limit)
            )
        )
        refreshed = 0
        for project_id in project_ids:
            if project_id is None:
                continue
            failed_task_id = await self.session.scalar(
                select(Task.id)
                .where(
                    Task.project_id == project_id,
                    Task.kind == TaskKind.DEVELOPMENT,
                    Task.status == TaskStatus.FAILED,
                )
                .limit(1)
            )
            # Preserve terminal failures as incident evidence. A later clean Task
            # bootstraps the workspace through its own execution path.
            if failed_task_id is not None:
                continue
            await self.detect(project_id, commit=False, create_workspace=False)
            refreshed += 1
        return refreshed

    @staticmethod
    def available_actions(profile: ProjectDevelopmentProfile) -> tuple[DevelopmentAction, ...]:
        """Return the one canonical ordered view of actions exposed by a persisted profile."""
        return tuple(
            action
            for action in (
                profile.install_action,
                profile.test_action,
                profile.build_action,
                profile.lint_action,
                profile.typecheck_action,
            )
            if action is not None
        )

    @staticmethod
    def _detect_files(workspace: Path) -> dict[str, object]:
        package = workspace / "package.json"
        if package.is_file() and not package.is_symlink():
            manifest_valid = True
            try:
                data = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
                manifest_valid = False
            if not isinstance(data, dict):
                data = {}
                manifest_valid = False
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            scripts = scripts if isinstance(scripts, dict) else {}

            def has_script(name: str) -> bool:
                value = scripts.get(name)
                return isinstance(value, str) and bool(value.strip())

            unsupported_manager = None
            if (workspace / "pnpm-lock.yaml").is_file():
                unsupported_manager = "pnpm"
            elif (workspace / "yarn.lock").is_file():
                unsupported_manager = "yarn"
            npm_supported = unsupported_manager is None
            return {
                "project_type": DevelopmentProjectType.NODE,
                "package_manager": PackageManager.NPM if npm_supported else PackageManager.NONE,
                "install_action": (
                    DevelopmentAction.NODE_INSTALL
                    if npm_supported and (workspace / "package-lock.json").is_file()
                    else None
                ),
                "test_action": (
                    DevelopmentAction.NODE_TEST if npm_supported and has_script("test") else None
                ),
                "build_action": (
                    DevelopmentAction.NODE_BUILD if npm_supported and has_script("build") else None
                ),
                "lint_action": (
                    DevelopmentAction.NODE_LINT if npm_supported and has_script("lint") else None
                ),
                "typecheck_action": (
                    DevelopmentAction.NODE_TYPECHECK
                    if npm_supported and has_script("typecheck")
                    else None
                ),
                "detection_source": (
                    "package.json (invalid JSON)"
                    if not manifest_valid
                    else "package.json"
                    if npm_supported
                    else f"package.json ({unsupported_manager} lockfile unsupported)"
                ),
            }
        if (workspace / "pyproject.toml").is_file() or (workspace / "requirements.txt").is_file():
            return {
                "project_type": DevelopmentProjectType.PYTHON,
                "package_manager": PackageManager.PIP,
                "install_action": (
                    DevelopmentAction.PYTHON_INSTALL
                    if (workspace / "requirements.txt").is_file()
                    else None
                ),
                "test_action": DevelopmentAction.PYTHON_TEST,
                "build_action": None,
                "lint_action": DevelopmentAction.PYTHON_LINT,
                "typecheck_action": None,
                "detection_source": (
                    "pyproject.toml"
                    if (workspace / "pyproject.toml").is_file()
                    else "requirements.txt"
                ),
            }
        if (workspace / "index.html").is_file():
            return {
                "project_type": DevelopmentProjectType.STATIC_WEB,
                "package_manager": PackageManager.NONE,
                "install_action": None,
                "test_action": None,
                "build_action": None,
                "lint_action": None,
                "typecheck_action": None,
                "detection_source": "index.html",
            }
        return {
            "project_type": DevelopmentProjectType.UNKNOWN,
            "package_manager": PackageManager.NONE,
            "install_action": None,
            "test_action": None,
            "build_action": None,
            "lint_action": None,
            "typecheck_action": None,
            "detection_source": "no supported manifest",
        }

    @staticmethod
    def supported_markers(workspace: Path) -> list[str]:
        markers = (
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "pyproject.toml",
            "requirements.txt",
            "index.html",
        )
        return [name for name in markers if (workspace / name).is_file()]
