import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import select

from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.contracts import RunnerRequest, RunnerResponse
from app.development.profile import DevelopmentProfileService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    DevelopmentProjectType,
    QADecision,
    TaskStatus,
    ToolCallStatus,
    ToolRiskLevel,
)
from app.domain.models import AcceptanceVerification, ProjectDevelopmentProfile, Task, ToolCall
from app.infrastructure.database import get_session_factory
from app.services.task_state_machine import TaskStateMachine
from app.tool_system.contracts import ToolDefinition, ToolExecutionContext
from app.tool_system.filesystem import FilesystemWriteInput, FilesystemWriteOutput
from app.tool_system.registry import ToolRegistry
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import create_agent, create_company, create_project, transition_task


class SimulatedControlledRunner:
    """Test-only adapter that implements only the same fixed action enum as the real runner."""

    def __init__(self, root: Path, *, fail_actions: set[DevelopmentAction] | None = None) -> None:
        self.root = root
        self.fail_actions = fail_actions or set()
        self.requests: list[RunnerRequest] = []
        self.snapshots: dict[str, dict[str, str]] = {}

    async def execute(self, request: RunnerRequest) -> RunnerResponse:
        self.requests.append(request)
        workspace = self.root / request.workspace_relative
        workspace.mkdir(parents=True, exist_ok=True)
        if request.action in self.fail_actions:
            return self._response(
                request,
                DevelopmentExecutionStatus.TIMED_OUT,
                None,
                code="DEVELOPMENT_RUNNER_UNAVAILABLE",
                message="The controlled runner is unavailable.",
            )
        if request.action == DevelopmentAction.GIT_INIT:
            (workspace / ".git").mkdir(exist_ok=True)
            return self._response(request, DevelopmentExecutionStatus.SUCCEEDED, 0)
        if request.action == DevelopmentAction.GIT_STATUS:
            if not (workspace / ".git").is_dir():
                return self._response(
                    request,
                    DevelopmentExecutionStatus.FAILED,
                    128,
                    stderr="fatal: not a git repository",
                    code="DEVELOPMENT_COMMAND_FAILED",
                    message="Development action exited non-zero.",
                )
            current = self._snapshot(workspace)
            baseline = self.snapshots.get(request.workspace_relative, {})
            changed = sorted(path for path, value in current.items() if baseline.get(path) != value)
            removed = sorted(path for path in baseline if path not in current)
            output = "\n".join(
                [*(f"?? {path}" for path in changed), *(f" D {path}" for path in removed)]
            )
            return self._response(
                request, DevelopmentExecutionStatus.SUCCEEDED, 0, stdout=output
            )
        if request.action == DevelopmentAction.GIT_CHECKPOINT:
            self.snapshots[request.workspace_relative] = self._snapshot(workspace)
            return self._response(request, DevelopmentExecutionStatus.SUCCEEDED, 0)
        if request.action in {
            DevelopmentAction.NODE_TEST,
            DevelopmentAction.NODE_BUILD,
            DevelopmentAction.NODE_LINT,
            DevelopmentAction.NODE_TYPECHECK,
        }:
            script = {
                DevelopmentAction.NODE_TEST: "test",
                DevelopmentAction.NODE_BUILD: "build",
                DevelopmentAction.NODE_LINT: "lint",
                DevelopmentAction.NODE_TYPECHECK: "typecheck",
            }[request.action]
            package = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
            if script not in package.get("scripts", {}):
                return self._response(
                    request,
                    DevelopmentExecutionStatus.FAILED,
                    1,
                    code="DEVELOPMENT_ACTION_UNSUPPORTED",
                    message=f"package.json has no {script} script",
                )
            return self._response(request, DevelopmentExecutionStatus.SUCCEEDED, 0, stdout="ok")
        raise AssertionError(f"Unexpected controlled action: {request.action}")

    @staticmethod
    def _snapshot(workspace: Path) -> dict[str, str]:
        return {
            path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
            for path in workspace.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(workspace).parts
        }

    @staticmethod
    def _response(
        request: RunnerRequest,
        status: DevelopmentExecutionStatus,
        exit_code: int | None,
        *,
        stdout: str = "",
        stderr: str = "",
        code: str | None = None,
        message: str | None = None,
    ) -> RunnerResponse:
        return RunnerResponse(
            request_id=request.request_id,
            status=status,
            exit_code=exit_code,
            stdout_excerpt=stdout,
            stderr_excerpt=stderr,
            stdout_bytes=len(stdout.encode()),
            stderr_bytes=len(stderr.encode()),
            duration_ms=2,
            error_code=code,
            error_message=message,
        )


async def create_development_domain(
    client: AsyncClient, tmp_path: Path, *, criteria: list[str] | None = None
) -> tuple[dict[str, object], dict[str, object], dict[str, object], Settings]:
    company = await create_company(client, slug=f"bootstrap-{uuid4().hex[:8]}")
    project = await create_project(client, str(company["id"]))
    developer = await create_agent(
        client,
        str(company["id"]),
        role="DEVELOPER",
        permissions={"filesystem.write": True, "development.execute": True, "git.read": True},
    )
    await create_agent(
        client,
        str(company["id"]),
        role="QA",
        permissions={"development.execute": True, "git.read": True},
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Bootstrap a supported software workspace",
            "acceptance_criteria": criteria
            or ["`manifest.json` exists", "npm test exits 0", "npm build exits 0"],
            "max_iterations": 4,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(
        tool_workspace_root=str(tmp_path / "workspaces"),
        model_provider="mock",
        allow_paid_model_calls=False,
        development_bootstrap_enabled=True,
        development_bootstrap_max_attempts=3,
    )
    return task, company, project, settings


def write_clipmind_fixture(workspace: Path, *, scripts: bool = True) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    package = {"name": "clipmind", "scripts": {}}
    if scripts:
        package["scripts"] = {"test": "node --test", "build": "node scripts/build.mjs"}
    (workspace / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (workspace / "manifest.json").write_text("{}", encoding="utf-8")
    (workspace / "sidepanel.html").write_text("<main>ClipMind</main>", encoding="utf-8")
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "capture.mjs").write_text(
        "export const capture = () => '';", encoding="utf-8"
    )
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "tests" / "capture.test.mjs").write_text("// deterministic test", encoding="utf-8")


async def start_task_run(task_id: str) -> UUID:
    async with get_session_factory()() as session:
        task = await TaskStateMachine(session).transition(
            UUID(task_id), TaskStatus.IN_PROGRESS, "Test execution started"
        )
        run = await TaskStateMachine(session).runs.get_active_for_task(task.id)
        assert run is not None
        return run.id


async def test_bootstrap_repairs_stale_unknown_profile_and_is_idempotent(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(client, tmp_path)
    manager = WorkspaceManager(settings.tool_workspace_root)
    workspace = manager.project_workspace(UUID(str(company["id"])), UUID(str(project["id"])))
    async with get_session_factory()() as session:
        stale = await DevelopmentProfileService(session, settings).detect(UUID(str(project["id"])))
        assert stale.project_type == DevelopmentProjectType.UNKNOWN
    write_clipmind_fixture(workspace)
    (workspace / ".env").write_text("OPENAI_API_KEY=should-never-be-inspected", encoding="utf-8")
    runner = SimulatedControlledRunner(Path(settings.tool_workspace_root))
    async with get_session_factory()() as session:
        profile = await ProjectBootstrapService(
            session, settings=settings, runner=runner
        ).ensure_task(UUID(str(task["id"])), checkpoint=True)
        assert profile.project_type == DevelopmentProjectType.NODE
        assert profile.package_manager.value == "NPM"
        assert profile.test_action == DevelopmentAction.NODE_TEST
        assert profile.build_action == DevelopmentAction.NODE_BUILD
        assert profile.repository_initialized is True
        assert profile.initial_checkpoint_created is True
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings, runner=runner).ensure_task(
            UUID(str(task["id"])), checkpoint=True
        )
    actions = [request.action for request in runner.requests]
    assert actions.count(DevelopmentAction.GIT_INIT) == 1
    assert actions.count(DevelopmentAction.GIT_CHECKPOINT) == 1


async def test_empty_workspace_refreshes_unknown_to_node_after_filesystem_change(
    client: AsyncClient, tmp_path: Path
) -> None:
    _task, company, project, settings = await create_development_domain(client, tmp_path)
    manager = WorkspaceManager(settings.tool_workspace_root)
    workspace = manager.project_workspace(UUID(str(company["id"])), UUID(str(project["id"])))
    async with get_session_factory()() as session:
        service = DevelopmentProfileService(session, settings)
        before = await service.detect(UUID(str(project["id"])))
        assert before.project_type == DevelopmentProjectType.UNKNOWN
    manager.atomic_write(
        workspace / "package.json",
        b'{"scripts":{"test":"node --test","build":"node scripts/build.mjs"}}',
    )
    manager.atomic_write(workspace / "manifest.json", b"{}")
    async with get_session_factory()() as session:
        after = await DevelopmentProfileService(session, settings).detect(UUID(str(project["id"])))
        assert after.project_type == DevelopmentProjectType.NODE
        assert after.test_action == DevelopmentAction.NODE_TEST
        assert after.build_action == DevelopmentAction.NODE_BUILD


async def test_successful_filesystem_write_refreshes_profile_without_restart(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, project, settings = await create_development_domain(client, tmp_path)
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "package.json",
                    "content": json.dumps(
                        {"scripts": {"test": "node --test", "build": "node scripts/build.mjs"}}
                    ),
                },
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Created the Node project manifest.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        before = await DevelopmentProfileService(session, settings).detect(
            UUID(str(project["id"]))
        )
        assert before.project_type == DevelopmentProjectType.UNKNOWN
        await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(task["id"])), defer_review=True
        )
        after = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        call = await session.scalar(
            select(ToolCall).where(ToolCall.task_id == UUID(str(task["id"])))
        )
        assert after is not None and after.project_type == DevelopmentProjectType.NODE
        assert after.test_action == DevelopmentAction.NODE_TEST
        assert after.build_action == DevelopmentAction.NODE_BUILD
        assert call is not None
        assert call.result["profile_refresh"] == {
            "project_type": "NODE",
            "package_manager": "NPM",
            "available_actions": ["NODE_TEST", "NODE_BUILD"],
            "detection_source": "package.json",
        }
        assert "NODE_TEST" in provider.requests[1].user_prompt


async def test_invalid_package_write_returns_profile_warning_and_qa_precondition(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, project, settings = await create_development_domain(client, tmp_path)
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "package.json",
                    "content": '{"scripts":{"test":"node --test"}}}',
                },
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "The invalid manifest needs correction.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(task["id"])), defer_review=True
        )
        profile = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        call = await session.scalar(
            select(ToolCall).where(ToolCall.task_id == UUID(str(task["id"])))
        )
        preconditions = await ProjectBootstrapService(
            session,
            settings=settings,
            runner=SimulatedControlledRunner(Path(settings.tool_workspace_root)),
        ).prepare_for_qa(UUID(str(task["id"])))

        assert run.status.value == "SUCCEEDED"
        assert profile is not None
        assert profile.detection_source == "package.json (invalid JSON)"
        assert profile.test_action is None and profile.build_action is None
        assert call is not None
        assert call.result["profile_refresh"]["warning"] == {
            "code": "DEVELOPMENT_MANIFEST_INVALID",
            "message": "package.json is not valid JSON and must be corrected.",
        }
        assert "DEVELOPMENT_MANIFEST_INVALID" in provider.requests[1].user_prompt
        assert preconditions.implementation_errors[0]["code"] == "DEVELOPMENT_MANIFEST_INVALID"


async def test_missing_npm_scripts_are_structured_implementation_preconditions(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(client, tmp_path)
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    write_clipmind_fixture(workspace, scripts=False)
    runner = SimulatedControlledRunner(Path(settings.tool_workspace_root))
    async with get_session_factory()() as session:
        preconditions = await ProjectBootstrapService(
            session, settings=settings, runner=runner
        ).prepare_for_qa(UUID(str(task["id"])))
    assert preconditions.infrastructure_errors == ()
    assert {item["code"] for item in preconditions.implementation_errors} == {
        "DEVELOPMENT_ACTION_UNSUPPORTED"
    }
    assert {item["message"].split()[0] for item in preconditions.implementation_errors} == {
        "NODE_TEST",
        "NODE_BUILD",
    }


async def test_infrastructure_unverifiable_does_not_consume_fix_iterations(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, _project, settings = await create_development_domain(client, tmp_path)
    run_id = await start_task_run(str(task["id"]))
    runner = SimulatedControlledRunner(
        Path(settings.tool_workspace_root), fail_actions={DevelopmentAction.GIT_INIT}
    )
    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), run_id)
        stored = await session.get(Task, task["id"])
        assert result.failure_classification == "INFRASTRUCTURE_UNVERIFIABLE"
        assert result.failure_code == "DEVELOPMENT_RUNNER_UNAVAILABLE"
        assert stored is not None and stored.status == TaskStatus.FAILED
        assert stored.iteration == 1
        assert "Forge development runtime" in result.blocking_issues[0]["description"]
    assert [request.action for request in runner.requests].count(DevelopmentAction.GIT_INIT) == 3


async def test_implementation_failure_consumes_one_iteration_and_requeues(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(client, tmp_path)
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    write_clipmind_fixture(workspace)
    run_id = await start_task_run(str(task["id"]))
    runner = SimulatedControlledRunner(
        Path(settings.tool_workspace_root), fail_actions={DevelopmentAction.NODE_TEST}
    )
    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), run_id)
        stored = await session.get(Task, task["id"])
        assert result.failure_classification == "IMPLEMENTATION_FAILURE"
        assert stored is not None and stored.status == TaskStatus.QUEUED
        assert stored.iteration == 1


async def test_empty_development_workspace_cannot_pass_qa_with_git_status_only(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, _project, settings = await create_development_domain(
        client, tmp_path, criteria=["`manifest.json` exists"]
    )
    run_id = await start_task_run(str(task["id"]))
    runner = SimulatedControlledRunner(Path(settings.tool_workspace_root))

    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), run_id)
        stored = await session.get(Task, task["id"])

    assert result.decision == QADecision.FAIL
    assert result.failure_code == "DEVELOPMENT_MANIFEST_MISSING"
    assert any(
        check.get("error_code") == "DEVELOPMENT_MANIFEST_MISSING"
        for check in result.checks
    )
    assert any(check["name"] == "GIT_STATUS" for check in result.checks)
    assert stored is not None and stored.status != TaskStatus.REVIEW


async def test_node_git_status_without_deterministic_actions_cannot_pass_qa(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(
        client, tmp_path, criteria=["`manifest.json` exists"]
    )
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    write_clipmind_fixture(workspace, scripts=False)
    run_id = await start_task_run(str(task["id"]))

    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(
            session,
            settings=settings,
            runner=SimulatedControlledRunner(Path(settings.tool_workspace_root)),
        ).finalize(UUID(str(task["id"])), run_id)

    assert result.decision == QADecision.FAIL
    assert any(
        check.get("error_code") == "DEVELOPMENT_VERIFICATION_UNAVAILABLE"
        for check in result.checks
    )
    assert any(
        "Git status alone cannot verify" in check["evidence"] for check in result.checks
    )


async def test_file_criterion_cannot_hide_failed_profile_lint(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(
        client, tmp_path, criteria=["`manifest.json` exists"]
    )
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    write_clipmind_fixture(workspace, scripts=False)
    (workspace / "sidepanel.html").write_text(
        "<main><h1>ClipMind</h1></main>", encoding="utf-8"
    )
    (workspace / "package.json").write_text(
        json.dumps({"name": "clipmind", "scripts": {"lint": "node scripts/lint.mjs"}}),
        encoding="utf-8",
    )
    run_id = await start_task_run(str(task["id"]))
    runner = SimulatedControlledRunner(
        Path(settings.tool_workspace_root), fail_actions={DevelopmentAction.NODE_LINT}
    )

    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), run_id)
        verifications = list(
            await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == UUID(str(task["id"]))
                )
            )
        )

    assert result.decision == QADecision.FAIL
    assert verifications[0].status == AcceptanceVerificationStatus.PASSED
    assert any(
        check["name"] == "NODE_LINT" and check["status"] == "FAILED"
        for check in result.checks
    )


async def test_qa_fix_requeue_runs_developer_before_reusing_acceptance_evidence(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, company, project, settings = await create_development_domain(
        client, tmp_path, criteria=["`manifest.json` exists"]
    )
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    write_clipmind_fixture(workspace, scripts=False)
    (workspace / "sidepanel.html").write_text(
        "<main><h1>ClipMind</h1></main>", encoding="utf-8"
    )
    (workspace / "package.json").write_text(
        json.dumps({"name": "clipmind", "scripts": {"lint": "node scripts/lint.mjs"}}),
        encoding="utf-8",
    )
    runner = SimulatedControlledRunner(
        Path(settings.tool_workspace_root), fail_actions={DevelopmentAction.NODE_LINT}
    )

    async with get_session_factory()() as session:
        first_run = await AgentRuntime(
            session, settings=settings, provider=MockModelProvider()
        ).execute(UUID(str(task["id"])), defer_review=True)
        first_qa = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), first_run.task_run_id)
        assert first_qa.decision == QADecision.FAIL
        assert all(
            verification.status == AcceptanceVerificationStatus.PASSED
            for verification in await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == UUID(str(task["id"])),
                    AcceptanceVerification.iteration == 1,
                )
            )
        )

    runner.fail_actions.clear()
    fixing_provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "src/qa-fix.mjs",
                    "content": "export const fixed = true;\n",
                },
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Applied the QA-requested fix.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        second_run = await AgentRuntime(
            session, settings=settings, provider=fixing_provider
        ).execute(UUID(str(task["id"])), defer_review=True)
        second_qa = await DevelopmentWorkflowService(
            session, settings=settings, runner=runner
        ).finalize(UUID(str(task["id"])), second_run.task_run_id)
        stored = await session.get(Task, task["id"])

    assert fixing_provider.call_count == 2
    assert (workspace / "src" / "qa-fix.mjs").is_file()
    assert second_qa.decision == QADecision.PASS
    assert stored is not None and stored.iteration == 2 and stored.status == TaskStatus.REVIEW


async def test_unresolved_write_runtime_failure_is_infrastructure_unverifiable(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, _project, settings = await create_development_domain(client, tmp_path)

    async def broken_write(
        arguments: BaseModel, context: ToolExecutionContext
    ) -> FilesystemWriteOutput:
        del arguments, context
        raise RuntimeError("simulated Forge filesystem adapter failure")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="filesystem.write",
            description="Test-only failing Forge write adapter",
            input_model=FilesystemWriteInput,
            output_model=FilesystemWriteOutput,
            risk_level=ToolRiskLevel.MEDIUM,
            permission_required="filesystem.write",
            timeout_seconds=1,
            enabled=True,
            handler=broken_write,
        )
    )
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "package.json", "content": '{"scripts":{}}'},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "The Forge write adapter failed.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        final_run = await AgentRuntime(
            session,
            settings=settings,
            provider=provider,
            tool_registry=registry,
        ).execute(UUID(str(task["id"])), defer_review=True)
        result = await DevelopmentWorkflowService(
            session,
            settings=settings,
            runner=SimulatedControlledRunner(Path(settings.tool_workspace_root)),
        ).finalize(UUID(str(task["id"])), final_run.task_run_id)
        stored = await session.get(Task, task["id"])
        call = await session.scalar(
            select(ToolCall).where(ToolCall.task_id == UUID(str(task["id"])))
        )

        assert result.failure_classification == "INFRASTRUCTURE_UNVERIFIABLE"
        assert result.failure_code == "TOOL_EXECUTION_FAILED"
        assert "filesystem.write could not durably complete" in result.summary
        assert stored is not None and stored.status == TaskStatus.FAILED
        assert stored.iteration == 1
        assert call is not None and call.status == ToolCallStatus.FAILED
        assert call.error == {
            "code": "TOOL_EXECUTION_FAILED",
            "message": "Tool execution failed",
        }


async def test_stale_profile_reconciliation_refreshes_active_projects(
    client: AsyncClient, tmp_path: Path
) -> None:
    _task, company, project, settings = await create_development_domain(client, tmp_path)
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    async with get_session_factory()() as session:
        stale = await DevelopmentProfileService(session, settings).detect(UUID(str(project["id"])))
        assert stale.project_type == DevelopmentProjectType.UNKNOWN
    write_clipmind_fixture(workspace)
    async with get_session_factory()() as session:
        refreshed = await DevelopmentProfileService(session, settings).reconcile_active()
        profile = await session.get(ProjectDevelopmentProfile, stale.id)
        assert refreshed == 1
        assert profile is not None and profile.project_type == DevelopmentProjectType.NODE


async def test_reconciliation_does_not_create_missing_workspace_on_read_only_path(
    client: AsyncClient, tmp_path: Path
) -> None:
    _task, _company, project, settings = await create_development_domain(client, tmp_path)
    workspace_root = Path(settings.tool_workspace_root)
    assert not workspace_root.exists()
    async with get_session_factory()() as session:
        refreshed = await DevelopmentProfileService(session, settings).reconcile_active()
        profile = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        assert refreshed == 1
        assert profile is not None and profile.project_type == DevelopmentProjectType.UNKNOWN
    assert not workspace_root.exists()


async def test_concurrent_bootstrap_initializes_repository_once(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _company, _project, settings = await create_development_domain(client, tmp_path)
    runner = SimulatedControlledRunner(Path(settings.tool_workspace_root))

    async def bootstrap() -> None:
        async with get_session_factory()() as session:
            await ProjectBootstrapService(session, settings=settings, runner=runner).ensure_task(
                UUID(str(task["id"]))
            )

    await asyncio.gather(bootstrap(), bootstrap())
    assert [request.action for request in runner.requests].count(DevelopmentAction.GIT_INIT) == 1
