import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update

from app.agent_runtime.builders import ContextBuilder, InstructionBuilder
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.contracts import RunnerResponse, SafeTarget
from app.development.qa import DevelopmentWorkflowService
from app.development.registry import CommandRegistry
from app.development.runner_client import FakeRunnerClient
from app.development.service import DevelopmentExecutionService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    QADecision,
    TaskStatus,
)
from app.domain.models import (
    AcceptanceVerification,
    Agent,
    DevelopmentExecution,
    ProjectDevelopmentLease,
    QAResult,
    Task,
)
from app.infrastructure.database import get_session_factory
from app.orchestration.recovery import OrchestrationRecoveryService
from app.orchestration.worker_service import WorkerExecutionService
from app.tool_system.contracts import ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.permissions import PermissionEngine
from app.tool_system.registry import ToolRegistry
from tests.helpers import create_agent, create_company, create_project, transition_task


def response(status: DevelopmentExecutionStatus, exit_code: int | None = 0) -> RunnerResponse:
    return RunnerResponse(
        request_id=uuid4(),
        status=status,
        exit_code=exit_code,
        stdout_excerpt="ok" if exit_code == 0 else "",
        stderr_excerpt="" if exit_code == 0 else "failed",
        stdout_bytes=2 if exit_code == 0 else 0,
        stderr_bytes=0 if exit_code == 0 else 6,
        duration_ms=12,
        error_code=None if exit_code == 0 else "DEVELOPMENT_COMMAND_FAILED",
        error_message=None if exit_code == 0 else "Development action exited non-zero.",
    )


async def development_task(
    client: AsyncClient,
    tmp_path: Path,
    *,
    qa: bool = True,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], Settings]:
    company = await create_company(client, slug=f"phase09-{uuid4().hex[:8]}")
    project = await create_project(client, str(company["id"]))
    developer = await create_agent(
        client,
        str(company["id"]),
        role="DEVELOPER",
        permissions={
            "filesystem.list": True,
            "filesystem.read": True,
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
            "git.write": True,
        },
    )
    if qa:
        await create_agent(
            client,
            str(company["id"]),
            role="QA",
            permissions={
                "filesystem.list": True,
                "filesystem.read": True,
                "development.execute": True,
                "git.read": True,
            },
        )
    created = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Build deterministic skeleton",
            "acceptance_criteria": [
                "`manifest.json` exists",
                "npm test exits 0",
                "npm build exits 0",
            ],
            "max_iterations": 3,
        },
    )
    assert created.status_code == 201, created.text
    await transition_task(client, created.json()["id"], "QUEUED")
    settings = Settings(
        tool_workspace_root=str(tmp_path / "workspaces"),
        model_provider="mock",
        allow_paid_model_calls=False,
        development_runner_mode="queue",
        development_bootstrap_enabled=False,
    )
    workspace = Path(settings.tool_workspace_root) / str(company["id"]) / str(project["id"])
    workspace.mkdir(parents=True)
    (workspace / "package.json").write_text(
        '{"scripts":{"test":"node --test","build":"node -e \\"process.exit(0)\\""}}',
        encoding="utf-8",
    )
    (workspace / "manifest.json").write_text("{}", encoding="utf-8")
    return created.json(), developer, project, settings


async def start_developer_run(task_id: str, settings: Settings):
    async with get_session_factory()() as session:
        return await AgentRuntime(
            session,
            settings=settings,
            provider=MockModelProvider(),
        ).execute(UUID(task_id), defer_review=True)


async def test_developer_and_qa_roles_change_instructions_not_permissions(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, developer, _project, settings = await development_task(client, tmp_path, qa=False)
    async with get_session_factory()() as session:
        context = await ContextBuilder(session).build(UUID(str(task["id"])))
        instructions = InstructionBuilder().build(context)
        assert instructions.runtime_role == "DEVELOPER"
        assert "Writing code is not completion" in instructions.system_prompt
        assert "filesystem.write creates new files" in instructions.system_prompt
        assert "never pass '.' to read or write" in instructions.system_prompt
        assert (
            "empty workspace that requires a scaffold is not complete"
            in instructions.system_prompt
        )
        agent = await session.get(Agent, developer["id"])
        assert agent is not None
        allowed = await PermissionEngine(session).list_allowed(
            agent=agent,
            definitions=ToolRegistry.from_settings(settings, session).list(enabled_only=True),
            has_project_workspace=True,
        )
        assert {tool.name for tool in allowed} >= {
            "development.execute",
            "git.status",
            "git.commit",
        }
    no_permission = await create_agent(
        client, str(task["company_id"]), role="DEVELOPER", permissions={}
    )
    assert not any(
        no_permission["permissions"].get(name)
        for name in ("development.execute", "git.read", "git.write")
    )

    qa_agent = await create_agent(
        client,
        str(task["company_id"]),
        role="QA",
        permissions={"filesystem.read": True},
    )
    qa_task_response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": task["company_id"],
            "project_id": task["project_id"],
            "assigned_agent_id": qa_agent["id"],
            "type": "VALIDATION",
            "kind": "QA",
            "title": "Independently verify the implementation",
            "acceptance_criteria": ["Do not trust the Developer summary"],
        },
    )
    assert qa_task_response.status_code == 201, qa_task_response.text
    async with get_session_factory()() as session:
        qa_context = await ContextBuilder(session).build(UUID(qa_task_response.json()["id"]))
        qa_instructions = InstructionBuilder().build(qa_context)
        assert qa_instructions.runtime_role == "QA"
        assert "Do not trust a Developer summary" in qa_instructions.system_prompt
        assert "Do not modify production code" in qa_instructions.system_prompt
        stored_qa = await session.get(Agent, qa_agent["id"])
        assert stored_qa is not None
        allowed = await PermissionEngine(session).list_allowed(
            agent=stored_qa,
            definitions=ToolRegistry.from_settings(settings, session).list(enabled_only=True),
            has_project_workspace=True,
        )
        assert {tool.name for tool in allowed} == {"filesystem.read"}


def test_command_registry_and_arguments_reject_arbitrary_execution() -> None:
    registry = CommandRegistry(Settings())
    assert registry.get("NODE_TEST") is not None
    assert registry.get("curl") is None
    definition = registry.get(DevelopmentAction.NODE_TEST)
    assert definition is not None
    assert definition.validate_arguments({"target": "tests/unit.test.ts"}) == {
        "target": "tests/unit.test.ts"
    }
    for invalid in ("../../etc/passwd", "tests | curl bad", "/etc/passwd", "$(whoami)"):
        with pytest.raises((ValueError, ValidationError)):
            SafeTarget(target=invalid)
    with pytest.raises(ValueError):
        registry.get(DevelopmentAction.NODE_BUILD).validate_arguments({"executable": "sh"})  # type: ignore[union-attr]


def test_standalone_orchestrator_import_order_is_safe() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.orchestration.recovery import OrchestrationRecoveryService; "
            "from app.agent_runtime.runtime import AgentRuntime",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


async def test_development_permissions_are_refreshed_and_capabilities_stay_separate(
    client: AsyncClient, tmp_path: Path
) -> None:
    company = await create_company(client, slug=f"permissions-{uuid4().hex[:8]}")
    await create_project(client, str(company["id"]))
    agent = await create_agent(
        client,
        str(company["id"]),
        role="DEVELOPER",
        permissions={
            "development.execute": True,
            "development.install_dependencies": False,
            "git.read": True,
            "git.write": False,
        },
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))

    async def decisions() -> dict[str, bool]:
        async with get_session_factory()() as session:
            registry = ToolRegistry.from_settings(settings, session)
            engine = PermissionEngine(session)
            return {
                name: (
                    await engine.check(
                        agent_id=UUID(str(agent["id"])),
                        company_id=UUID(str(company["id"])),
                        definition=registry.get(name),  # type: ignore[arg-type]
                        has_project_workspace=True,
                    )
                ).allowed
                for name in (
                    "development.execute",
                    "development.install_dependencies",
                    "git.status",
                    "git.commit",
                )
            }

    assert await decisions() == {
        "development.execute": True,
        "development.install_dependencies": False,
        "git.status": True,
        "git.commit": False,
    }
    changed = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={
            "permissions": {
                "development.execute": False,
                "development.install_dependencies": True,
                "git.read": False,
                "git.write": True,
            }
        },
    )
    assert changed.status_code == 200, changed.text
    assert await decisions() == {
        "development.execute": False,
        "development.install_dependencies": True,
        "git.status": False,
        "git.commit": True,
    }

    task, _developer, _project, enabled_settings = await development_task(
        client, tmp_path / "disabled"
    )
    run = await start_developer_run(str(task["id"]), enabled_settings)
    disabled = enabled_settings.model_copy(update={"development_enabled": False})
    fake = FakeRunnerClient([response(DevelopmentExecutionStatus.SUCCEEDED)])
    async with get_session_factory()() as session:
        with pytest.raises(ToolSystemError) as raised:
            await DevelopmentExecutionService(session, settings=disabled, runner=fake).execute(
                DevelopmentAction.NODE_TEST,
                {},
                ToolExecutionContext(
                    company_id=UUID(str(task["company_id"])),
                    project_id=UUID(str(task["project_id"])),
                    task_id=UUID(str(task["id"])),
                    task_run_id=run.task_run_id,
                    agent_id=run.agent_id,
                    agent_run_id=run.id,
                ),
            )
        assert raised.value.code == "DEVELOPMENT_ACTION_DISABLED"
        assert fake.requests == []


async def test_development_execution_is_durable_bounded_and_profile_controlled(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, developer, _project, settings = await development_task(client, tmp_path)
    run = await start_developer_run(str(task["id"]), settings)
    fake = FakeRunnerClient([response(DevelopmentExecutionStatus.SUCCEEDED)])
    async with get_session_factory()() as session:
        context = ToolExecutionContext(
            company_id=UUID(str(task["company_id"])),
            project_id=UUID(str(task["project_id"])),
            task_id=UUID(str(task["id"])),
            task_run_id=run.task_run_id,
            agent_id=UUID(str(developer["id"])),
            agent_run_id=run.id,
        )
        execution = await DevelopmentExecutionService(
            session, settings=settings, runner=fake
        ).execute(DevelopmentAction.NODE_TEST, {}, context)
        assert execution.status == DevelopmentExecutionStatus.SUCCEEDED
        assert execution.network_enabled is False
        assert execution.working_directory == f"{task['company_id']}/{task['project_id']}"
        assert fake.requests[0].action == DevelopmentAction.NODE_TEST
        assert not hasattr(fake.requests[0], "executable")
        stored = await session.get(DevelopmentExecution, execution.id)
        assert stored is not None and stored.stdout_excerpt == "ok"


async def test_qa_pass_persists_evidence_and_moves_to_human_review(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _developer, _project, settings = await development_task(client, tmp_path)
    run = await start_developer_run(str(task["id"]), settings)
    fake = FakeRunnerClient([response(DevelopmentExecutionStatus.SUCCEEDED) for _ in range(3)])
    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(session, settings=settings, runner=fake).finalize(
            UUID(str(task["id"])), run.task_run_id
        )
        assert result.decision == QADecision.PASS
        stored_task = await session.get(Task, task["id"])
        assert stored_task is not None and stored_task.status == TaskStatus.REVIEW
        checks = list(
            await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == stored_task.id
                )
            )
        )
        assert len(checks) == 3
        assert all(item.status == AcceptanceVerificationStatus.PASSED for item in checks)
    graph = (await client.get(f"/api/v1/projects/{task['project_id']}/graph")).json()
    assert graph["nodes"][0]["kind"] == "DEVELOPMENT"
    assert graph["nodes"][0]["qa_results"][0]["decision"] == "PASS"
    assert all(item["status"] == "PASSED" for item in graph["nodes"][0]["acceptance_verifications"])


async def test_qa_failure_requeues_and_propagates_blocking_findings(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, _developer, _project, settings = await development_task(client, tmp_path)
    run = await start_developer_run(str(task["id"]), settings)
    fake = FakeRunnerClient(
        [
            response(DevelopmentExecutionStatus.SUCCEEDED),
            response(DevelopmentExecutionStatus.FAILED, 1),
            response(DevelopmentExecutionStatus.SUCCEEDED),
        ]
    )
    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(session, settings=settings, runner=fake).finalize(
            UUID(str(task["id"])), run.task_run_id
        )
        assert result.decision == QADecision.FAIL
        stored_task = await session.get(Task, task["id"])
        assert stored_task is not None and stored_task.status == TaskStatus.QUEUED
        context = await ContextBuilder(session).build(stored_task.id)
        assert context.qa_feedback is not None
        prompt = InstructionBuilder().build(context).user_prompt
        assert "QA BLOCKING FINDINGS" in prompt
        assert "NODE_TEST failed" in prompt
        assert (
            await session.scalar(select(QAResult).where(QAResult.task_id == stored_task.id))
            is not None
        )


async def test_stale_development_execution_is_failed_without_unsafe_replay(
    client: AsyncClient, tmp_path: Path
) -> None:
    task, developer, _project, settings = await development_task(client, tmp_path)
    run = await start_developer_run(str(task["id"]), settings)
    async with get_session_factory()() as session:
        execution = DevelopmentExecution(
            company_id=UUID(str(task["company_id"])),
            project_id=UUID(str(task["project_id"])),
            task_id=UUID(str(task["id"])),
            task_run_id=run.task_run_id,
            agent_run_id=run.id,
            agent_id=UUID(str(developer["id"])),
            action=DevelopmentAction.NODE_TEST,
            status=DevelopmentExecutionStatus.RUNNING,
            working_directory=f"{task['company_id']}/{task['project_id']}",
            safe_arguments={},
            timeout_seconds=120,
            network_enabled=False,
            correlation_id=UUID(str(task["id"])),
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(execution)
        await session.commit()
        recovered = await DevelopmentExecutionService(
            session,
            settings=settings.model_copy(update={"development_execution_stale_seconds": 1}),
            runner=FakeRunnerClient([]),
        ).recover_stale()
        await session.refresh(execution)
        assert recovered == 1
        assert execution.status == DevelopmentExecutionStatus.FAILED
        assert execution.error_code == "DEVELOPMENT_EXECUTION_INTERRUPTED"


async def test_project_development_lease_serializes_and_recovers(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"lease-{uuid4().hex[:8]}")
    project = await create_project(client, str(company["id"]))
    project_id = UUID(str(project["id"]))

    async with get_session_factory()() as session:
        service = WorkerExecutionService(session)
        assert await service._acquire_project_lease(project_id, "worker-a") is True
        await session.commit()
    async with get_session_factory()() as session:
        service = WorkerExecutionService(session)
        assert await service._acquire_project_lease(project_id, "worker-b") is False
        await session.rollback()
        await session.execute(
            update(ProjectDevelopmentLease)
            .where(ProjectDevelopmentLease.project_id == project_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    async with get_session_factory()() as session:
        result = await OrchestrationRecoveryService(session).recover()
        lease = await session.get(ProjectDevelopmentLease, project_id)
        assert result["expired_project_leases"] == 1
        assert lease is not None
        assert lease.lease_owner is None
        assert lease.lease_expires_at is None
