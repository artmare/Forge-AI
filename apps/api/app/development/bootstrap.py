from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.contracts import RunnerRequest, RunnerResponse
from app.development.profile import DevelopmentProfileService
from app.development.registry import CommandRegistry
from app.development.runner_client import RunnerClient, runner_client_from_settings
from app.development.self_workflow import SelfDevelopmentWorkflow
from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus, DevelopmentProjectType
from app.domain.exceptions import DevelopmentInfrastructureError, EntityNotFoundError
from app.domain.models import Project, ProjectDevelopmentProfile, Task
from app.services.event_factory import EventFactory
from app.tool_system.workspace import WorkspaceManager


@dataclass(frozen=True)
class DevelopmentPreconditions:
    profile: ProjectDevelopmentProfile
    infrastructure_errors: tuple[dict[str, str], ...]
    implementation_errors: tuple[dict[str, str], ...]


class ProjectBootstrapService:
    """Deterministically prepares a project workspace without exposing command text."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        runner: RunnerClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.runner = runner or runner_client_from_settings(self.settings)
        self.registry = CommandRegistry(self.settings)
        self.profiles = DevelopmentProfileService(session, self.settings)
        self.workspace = WorkspaceManager(self.settings.tool_workspace_root)
        self.events = EventFactory(session)

    async def ensure_task(
        self, task_id: UUID, *, checkpoint: bool = False
    ) -> ProjectDevelopmentProfile:
        task = await self.session.get(Task, task_id)
        if task is None:
            raise EntityNotFoundError("Task")
        if task.project_id is None or task.assigned_agent_id is None:
            raise DevelopmentInfrastructureError(
                "DEVELOPMENT_WORKSPACE_UNAVAILABLE",
                "Development Task has no project workspace or assigned Agent.",
            )
        project = await self.session.scalar(
            select(Project).where(Project.id == task.project_id).with_for_update()
        )
        if project is None:
            raise EntityNotFoundError("Project")
        if SelfDevelopmentWorkflow.requested(task):
            await SelfDevelopmentWorkflow(self.session, self.settings).prepare(task)
        workspace = self.workspace.project_workspace(project.company_id, project.id)
        profile = await self.profiles.detect(project.id, commit=False)
        if not self.settings.development_bootstrap_enabled:
            await self.session.commit()
            return profile

        profile.bootstrap_attempts += 1
        if not self._repository_marker_available(workspace):
            response = await self._run(task, DevelopmentAction.GIT_INIT)
            if response.status != DevelopmentExecutionStatus.SUCCEEDED:
                await self._fail_profile(
                    profile,
                    response.error_code or "DEVELOPMENT_GIT_INIT_FAILED",
                    response.error_message or "Forge could not initialize the local repository.",
                )
            profile.repository_initialized = True
            profile.repository_branch = "main"
            await self._event(
                task,
                "DEVELOPMENT_REPOSITORY_INITIALIZED",
                "Local Git repository initialized.",
            )
        else:
            profile.repository_initialized = True

        profile = await self.profiles.detect(project.id, commit=False)
        if checkpoint and not SelfDevelopmentWorkflow.requested(task):
            await self._checkpoint_initial_skeleton(task, profile)
        profile.bootstrap_error_code = None
        profile.bootstrap_error_message = None
        await self._event(
            task,
            "DEVELOPMENT_PROFILE_REFRESHED",
            f"Development profile refreshed as {profile.project_type.value}.",
        )
        await self.session.commit()
        return profile

    async def prepare_for_qa(self, task_id: UUID) -> DevelopmentPreconditions:
        profile = await self.ensure_task(task_id, checkpoint=True)
        if not self.settings.development_bootstrap_enabled:
            return DevelopmentPreconditions(profile, (), ())
        task = await self.session.get(Task, task_id)
        if task is None or task.project_id is None:
            raise EntityNotFoundError("Task")
        workspace = self.workspace.project_workspace(task.company_id, task.project_id)
        infrastructure: list[dict[str, str]] = []
        implementation: list[dict[str, str]] = []
        if not profile.repository_initialized:
            infrastructure.append(
                {
                    "code": "DEVELOPMENT_REPOSITORY_UNAVAILABLE",
                    "message": "Forge could not establish a usable local Git repository.",
                }
            )
        markers = self.profiles.supported_markers(workspace)
        if profile.project_type == DevelopmentProjectType.UNKNOWN:
            target = infrastructure if markers else implementation
            target.append(
                {
                    "code": (
                        "DEVELOPMENT_PROFILE_RECONCILIATION_FAILED"
                        if markers
                        else "DEVELOPMENT_MANIFEST_MISSING"
                    ),
                    "message": (
                        "Supported project markers exist but Forge could not reconcile the "
                        "development profile."
                        if markers
                        else "No supported software project manifest was created."
                    ),
                }
            )
        if profile.detection_source == "package.json (invalid JSON)":
            implementation.append(
                {
                    "code": "DEVELOPMENT_MANIFEST_INVALID",
                    "message": "package.json is not valid JSON and must be corrected.",
                }
            )
        for action, words in (
            (DevelopmentAction.NODE_TEST, ("test", "tests")),
            (DevelopmentAction.NODE_BUILD, ("build", "runnable")),
            (DevelopmentAction.NODE_LINT, ("lint",)),
            (DevelopmentAction.NODE_TYPECHECK, ("typecheck", "type check")),
        ):
            required = any(
                any(word in str(item).lower() for word in words)
                for item in task.acceptance_criteria
            )
            available = action in {
                profile.test_action,
                profile.build_action,
                profile.lint_action,
                profile.typecheck_action,
            }
            if profile.project_type == DevelopmentProjectType.NODE and required and not available:
                implementation.append(
                    {
                        "code": "DEVELOPMENT_ACTION_UNSUPPORTED",
                        "message": (
                            f"{action.value} is unavailable because package.json does not "
                            "declare the required script."
                        ),
                    }
                )
        return DevelopmentPreconditions(profile, tuple(infrastructure), tuple(implementation))

    async def _checkpoint_initial_skeleton(
        self, task: Task, profile: ProjectDevelopmentProfile
    ) -> None:
        status = await self._run(task, DevelopmentAction.GIT_STATUS)
        if status.status != DevelopmentExecutionStatus.SUCCEEDED:
            await self._fail_profile(
                profile,
                status.error_code or "DEVELOPMENT_REPOSITORY_UNAVAILABLE",
                status.error_message or "Repository status is unavailable after bootstrap.",
            )
        changed = self._changed_files(status.stdout_excerpt)
        profile.changed_files_count = changed
        if changed == 0 or profile.initial_checkpoint_created:
            return
        checkpoint = await self._run(task, DevelopmentAction.GIT_CHECKPOINT)
        if checkpoint.status != DevelopmentExecutionStatus.SUCCEEDED:
            await self._fail_profile(
                profile,
                checkpoint.error_code or "DEVELOPMENT_INITIAL_CHECKPOINT_FAILED",
                checkpoint.error_message or "Forge could not create the initial local checkpoint.",
            )
        profile.initial_checkpoint_created = True
        refreshed = await self._run(task, DevelopmentAction.GIT_STATUS)
        if refreshed.status != DevelopmentExecutionStatus.SUCCEEDED:
            await self._fail_profile(
                profile,
                refreshed.error_code or "DEVELOPMENT_REPOSITORY_UNAVAILABLE",
                refreshed.error_message or "Repository status is unavailable after checkpoint.",
            )
        profile.changed_files_count = self._changed_files(refreshed.stdout_excerpt)
        await self._event(
            task,
            "DEVELOPMENT_INITIAL_CHECKPOINT_CREATED",
            "Initial local development checkpoint created.",
        )

    async def _run(self, task: Task, action: DevelopmentAction) -> RunnerResponse:
        definition = self.registry.get(action)
        if definition is None or not definition.enabled:
            raise DevelopmentInfrastructureError(
                "DEVELOPMENT_BOOTSTRAP_ACTION_UNAVAILABLE",
                f"Forge bootstrap action {action.value} is unavailable.",
            )
        response: RunnerResponse | None = None
        for _attempt in range(max(self.settings.development_bootstrap_max_attempts, 1)):
            response = await self.runner.execute(
                RunnerRequest(
                    request_id=uuid4(),
                    action=action,
                    workspace_relative=f"{task.company_id}/{task.project_id}",
                    safe_arguments={},
                    timeout_seconds=definition.timeout_seconds,
                    output_limit_bytes=definition.output_limit_bytes,
                    task_id=task.id,
                )
            )
            if response.status not in {
                DevelopmentExecutionStatus.TIMED_OUT,
                DevelopmentExecutionStatus.CANCELLED,
            }:
                return response
        assert response is not None
        return response

    async def _fail_profile(
        self, profile: ProjectDevelopmentProfile, code: str, message: str
    ) -> None:
        profile.bootstrap_error_code = code
        profile.bootstrap_error_message = message
        await self.session.commit()
        raise DevelopmentInfrastructureError(code, message)

    async def _event(self, task: Task, kind: str, message: str) -> None:
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type=kind,
            message=message,
            payload={"project_id": str(task.project_id)},
        )

    @staticmethod
    def _repository_marker_available(workspace: Path) -> bool:
        git = workspace / ".git"
        return (git.is_dir() or git.is_file()) and not git.is_symlink()

    @staticmethod
    def _changed_files(output: str) -> int:
        return sum(1 for line in output.splitlines() if len(line) >= 3)
