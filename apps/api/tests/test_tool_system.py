import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.agent_runtime.contracts import ModelRequest, ModelResponse
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import AgentRunStatus, TaskStatus, ToolCallStatus, ToolRiskLevel
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import Agent, AgentRun, Event, Task, TaskRun, ToolCall
from app.infrastructure.database import get_session_factory
from app.services.task_state_machine import TaskStateMachine
from app.tool_system.contracts import ToolDefinition, ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.executor import ToolExecutor
from app.tool_system.filesystem import (
    FilesystemPathInput,
    FilesystemTools,
    FilesystemWriteInput,
)
from app.tool_system.recovery import StaleToolCallRecovery
from app.tool_system.registry import ToolRegistry
from app.tool_system.service import ToolExecutionService
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)

FILESYSTEM_PERMISSIONS = {
    "filesystem.list": True,
    "filesystem.read": True,
    "filesystem.write": True,
}


async def queued_tool_domain(
    client: AsyncClient, permissions: dict[str, bool] | None = None
) -> dict[str, dict[str, object]]:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="RESEARCHER",
        permissions=permissions or FILESYSTEM_PERMISSIONS,
    )
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        title="Create and verify note.txt",
    )
    await transition_task(client, task["id"], "QUEUED")
    return {"company": company, "project": project, "agent": agent, "task": task}


def execution_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        company_id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        task_run_id=uuid4(),
        agent_id=uuid4(),
        agent_run_id=uuid4(),
    )


async def test_registry_resolution_disabled_state_and_safe_schema(tmp_path: Path) -> None:
    settings = Settings(
        tool_workspace_root=str(tmp_path),
        filesystem_read_enabled=False,
        openai_api_key="phase06-test-openai-secret",
        database_url="postgresql+asyncpg://secret-user:secret-password@db/forge",
        redis_url="redis://:secret-password@redis:6379/0",
    )
    registry = ToolRegistry.from_settings(settings)

    assert registry.get("filesystem.list") is not None
    assert registry.get("missing") is None
    assert registry.get("filesystem.read").enabled is False  # type: ignore[union-attr]
    public = {definition.name: definition for definition in registry.public()}
    assert public["filesystem.write"].risk_level == ToolRiskLevel.MEDIUM
    assert public["filesystem.write"].permission_required == "filesystem.write"
    assert "content" in public["filesystem.write"].input_schema["properties"]
    write_contract = json.dumps(public["filesystem.write"].model_dump(mode="json"))
    assert "target need not exist" in write_contract
    assert "missing parent directories are created safely" in write_contract
    assert "Use path='.'" in public["filesystem.list"].description
    assert "Do not pass '.'" in public["filesystem.read"].description
    serialized = json.dumps([definition.model_dump(mode="json") for definition in public.values()])
    assert "phase06-test-openai-secret" not in serialized
    assert "secret-password" not in serialized

    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("filesystem.list"))  # type: ignore[arg-type]


async def test_agent_permissions_are_conservative_and_strict(client: AsyncClient) -> None:
    company = await create_company(client)
    agent = await create_agent(client, company["id"], permissions={})

    assert agent["permissions"] == {
        "filesystem.list": False,
        "filesystem.read": False,
        "filesystem.write": False,
        "shell.run": False,
        "development.execute": False,
        "development.install_dependencies": False,
        "git.read": False,
        "git.write": False,
    }
    invalid = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={"permissions": {"filesystem.delete": True}},
    )
    assert invalid.status_code == 422


async def test_filesystem_tools_and_security_boundaries(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    tools = FilesystemTools(manager, read_max_bytes=16, write_max_bytes=16)
    context = execution_context()

    written = await tools.write_file(
        FilesystemWriteInput(path="src/note.txt", content="Forge works"), context
    )
    package = await tools.write_file(
        FilesystemWriteInput(path="package.json", content='{"scripts":{}}'), context
    )
    read = await tools.read_file(FilesystemPathInput(path="src/note.txt"), context)
    listing = await tools.list_path(FilesystemPathInput(path="src"), context)

    assert written.path == "src/note.txt" and written.created is True
    assert package.path == "package.json" and package.created is True
    assert read.content == "Forge works" and read.byte_size == 11
    assert [(entry.name, entry.path, entry.type) for entry in listing.entries] == [
        ("note.txt", "src/note.txt", "file")
    ]
    assert str(tmp_path) not in listing.model_dump_json()

    for attack in (
        "../../secret.txt",
        "/etc/passwd",
        "/app/.env",
        r"C:\Users\admin\secret.txt",
    ):
        with pytest.raises(ToolSystemError) as raised:
            await tools.read_file(FilesystemPathInput(path=attack), context)
        assert raised.value.code == "PATH_OUTSIDE_WORKSPACE"
        with pytest.raises(ToolSystemError) as raised:
            await tools.write_file(FilesystemWriteInput(path=attack, content="blocked"), context)
        assert raised.value.code == "PATH_OUTSIDE_WORKSPACE"

    workspace = manager.project_workspace(context.company_id, context.project_id)  # type: ignore[arg-type]
    (workspace / "existing-directory").mkdir()
    with pytest.raises(ToolSystemError) as raised:
        await tools.write_file(
            FilesystemWriteInput(path="existing-directory", content="blocked"), context
        )
    assert raised.value.code == "UNSUPPORTED_FILE_TYPE"

    with pytest.raises(ToolSystemError) as raised:
        await tools.write_file(FilesystemWriteInput(path="large.txt", content="x" * 17), context)
    assert raised.value.code == "FILE_TOO_LARGE"

    (workspace / "large-read.txt").write_text("x" * 17, encoding="utf-8")
    with pytest.raises(ToolSystemError) as raised:
        await tools.read_file(FilesystemPathInput(path="large-read.txt"), context)
    assert raised.value.code == "FILE_TOO_LARGE"


async def test_symlink_escape_is_denied_when_supported(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    tools = FilesystemTools(manager, read_max_bytes=1024, write_max_bytes=1024)
    context = execution_context()
    workspace = manager.project_workspace(context.company_id, context.project_id)  # type: ignore[arg-type]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")

    with pytest.raises(ToolSystemError) as raised:
        await tools.read_file(FilesystemPathInput(path="escape/secret.txt"), context)
    assert raised.value.code == "SYMLINK_NOT_ALLOWED"
    with pytest.raises(ToolSystemError) as raised:
        await tools.write_file(
            FilesystemWriteInput(path="escape/new.txt", content="blocked"), context
        )
    assert raised.value.code == "SYMLINK_NOT_ALLOWED"


async def test_executor_normalizes_validation_timeout_and_output() -> None:
    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        value: str

    async def slow(arguments: BaseModel, context: ToolExecutionContext) -> BaseModel:
        del arguments, context
        await asyncio.sleep(0.05)
        return Output(value="late")

    definition = ToolDefinition(
        name="test.slow",
        description="Test timeout",
        input_model=Input,
        output_model=Output,
        risk_level=ToolRiskLevel.LOW,
        permission_required="filesystem.read",
        timeout_seconds=0.001,
        enabled=True,
        handler=slow,
    )
    executor = ToolExecutor()
    with pytest.raises(ToolSystemError) as raised:
        executor.validate_arguments(definition, {})
    assert raised.value.code == "TOOL_ARGUMENT_VALIDATION_FAILED"

    parsed = executor.validate_arguments(definition, {"value": "ok"})
    result = await executor.execute(definition, parsed, execution_context())
    assert result.status == "error"
    assert result.error is not None and result.error.code == "TOOL_TIMEOUT"


async def test_runtime_write_read_final_e2e_and_apis(client: AsyncClient, tmp_path: Path) -> None:
    domain = await queued_tool_domain(client)
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "note.txt", "content": "Forge works"},
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.read",
                "arguments": {"path": "note.txt"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Created and verified note.txt.",
                    "output": {"content": "Forge works"},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        final_run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(domain["task"]["id"]))
        )

    assert final_run.status == AgentRunStatus.SUCCEEDED
    assert final_run.response["output"]["content"] == "Forge works"
    workspace_file = (
        tmp_path
        / "workspaces"
        / str(domain["company"]["id"])
        / str(domain["project"]["id"])
        / "note.txt"
    )
    assert workspace_file.read_text(encoding="utf-8") == "Forge works"
    assert "filesystem.write" in provider.requests[0].system_prompt
    assert {tool.name for tool in provider.requests[0].tools} >= {
        "filesystem.read",
        "filesystem.write",
    }
    assert "Structured tool observations" in provider.requests[1].user_prompt
    assert "Forge works" in provider.requests[2].user_prompt

    task = (await client.get(f"/api/v1/tasks/{domain['task']['id']}")).json()
    agent_runs = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/agent-runs")).json()
    tool_calls = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/tool-calls")).json()
    tools = (await client.get("/api/v1/tools")).json()
    stats = (await client.get("/api/v1/tool-calls/stats")).json()
    recent = (await client.get("/api/v1/tool-calls?limit=2")).json()
    by_agent_run = (
        await client.get(f"/api/v1/agent-runs/{tool_calls[0]['agent_run_id']}/tool-calls")
    ).json()

    assert task["status"] == "REVIEW"
    assert len(agent_runs) == 3
    assert [call["status"] for call in reversed(tool_calls)] == ["SUCCEEDED", "SUCCEEDED"]
    assert {tool["name"] for tool in tools} == {
        "filesystem.list",
        "filesystem.read",
        "filesystem.write",
    }
    assert stats["total"] == 2 and stats["by_status"]["SUCCEEDED"] == 2
    assert recent[0]["agent_name"] == domain["agent"]["name"]
    assert by_agent_run[0]["id"] == tool_calls[0]["id"]

    async with get_session_factory()() as session:
        events = list(await session.scalars(select(Event).where(Event.task_id == task["id"])))
    event_types = {event.type for event in events}
    assert {
        "TOOL_CALL_REQUESTED",
        "TOOL_CALL_AUTHORIZED",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_SUCCEEDED",
    }.issubset(event_types)
    assert "Forge works" not in json.dumps([event.details for event in events])


async def test_permission_denial_is_observed_without_side_effect(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(
        client,
        permissions={
            "filesystem.list": True,
            "filesystem.read": True,
            "filesystem.write": False,
        },
    )
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "denied.txt", "content": "must not exist"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Write permission was denied.",
                    "output": {"written": False},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(domain["task"]["id"]))
        )

    calls = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/tool-calls")).json()
    assert calls[0]["status"] == "DENIED"
    assert calls[0]["error"]["code"] == "TOOL_PERMISSION_DENIED"
    assert not (
        tmp_path
        / "workspaces"
        / str(domain["company"]["id"])
        / str(domain["project"]["id"])
        / "denied.txt"
    ).exists()
    assert "filesystem.write" not in provider.requests[0].system_prompt
    assert "TOOL_PERMISSION_DENIED" in provider.requests[1].user_prompt


async def test_normal_tool_failure_can_recover_to_final(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.read",
                "arguments": {"path": "missing.txt"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "The optional file was absent.",
                    "output": {"found": False},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(domain["task"]["id"]))
        )

    task = (await client.get(f"/api/v1/tasks/{domain['task']['id']}")).json()
    calls = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/tool-calls")).json()
    assert task["status"] == "REVIEW"
    assert calls[0]["status"] == "FAILED"
    assert calls[0]["error"]["code"] == "FILE_NOT_FOUND"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "settings_override", "expected_code"),
    [
        ("filesystem.delete", {"path": "x"}, {}, "TOOL_NOT_FOUND"),
        (
            "filesystem.read",
            {"path": "x"},
            {"filesystem_read_enabled": False},
            "TOOL_DISABLED",
        ),
        (
            "filesystem.write",
            {"path": "x"},
            {},
            "TOOL_ARGUMENT_VALIDATION_FAILED",
        ),
    ],
)
async def test_invalid_unknown_and_globally_disabled_requests_are_observed(
    client: AsyncClient,
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
    settings_override: dict[str, bool],
    expected_code: str,
) -> None:
    domain = await queued_tool_domain(client)
    provider = MockModelProvider(
        responses=[
            {"type": "tool_call", "tool_name": tool_name, "arguments": arguments},
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Handled the rejected tool request.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"), **settings_override)
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(domain["task"]["id"]))
        )

    calls = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/tool-calls")).json()
    assert calls[0]["status"] == "FAILED"
    assert calls[0]["error"]["code"] == expected_code
    assert expected_code in provider.requests[1].user_prompt


async def test_observation_is_bounded_and_marks_truncation(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    content = "bounded-content-" * 30
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "large.txt", "content": content},
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.read",
                "arguments": {"path": "large.txt"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Observed bounded content.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(
        tool_workspace_root=str(tmp_path / "workspaces"),
        tool_observation_max_chars=180,
    )
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(str(domain["task"]["id"]))
        )

    final_prompt = provider.requests[2].user_prompt
    assert '"truncated":true' in final_prompt
    assert '"original_chars":' in final_prompt
    assert content not in final_prompt


async def test_max_tool_steps_fails_execution(client: AsyncClient, tmp_path: Path) -> None:
    domain = await queued_tool_domain(client)
    request = {
        "type": "tool_call",
        "tool_name": "filesystem.list",
        "arguments": {"path": "."},
    }
    provider = MockModelProvider(responses=[request])
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"), agent_max_tool_steps=1)
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=provider).execute(
                UUID(str(domain["task"]["id"]))
            )
    assert raised.value.code == "MAX_TOOL_STEPS_EXCEEDED"

    async with get_session_factory()() as session:
        task = await session.get(Task, domain["task"]["id"])
        assert task is not None and task.status == TaskStatus.FAILED
        assert await session.scalar(select(func.count()).select_from(ToolCall)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 2


async def test_permission_is_rechecked_after_revocation(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    task_id = UUID(str(domain["task"]["id"]))
    agent_id = UUID(str(domain["agent"]["id"]))

    class RevokingProvider:
        name = "mock"
        paid = False

        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls == 2:
                async with get_session_factory()() as other:
                    await other.execute(
                        update(Agent)
                        .where(Agent.id == agent_id)
                        .values(
                            permissions={
                                "filesystem.list": False,
                                "filesystem.read": False,
                                "filesystem.write": False,
                                "shell.run": False,
                            }
                        )
                    )
                    await other.commit()
            output = (
                {
                    "type": "tool_call",
                    "tool_name": "filesystem.list",
                    "arguments": {"path": "."},
                }
                if self.calls < 3
                else {
                    "type": "final",
                    "result": {
                        "status": "completed",
                        "summary": "Permission revocation was observed.",
                        "output": {},
                        "notes": [],
                    },
                }
            )
            return ModelResponse(output=output, provider="mock", model=request.model)

    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=RevokingProvider()).execute(task_id)

    calls = (await client.get(f"/api/v1/tasks/{domain['task']['id']}/tool-calls")).json()
    assert [call["status"] for call in reversed(calls)] == ["SUCCEEDED", "DENIED"]


async def test_cancellation_stops_future_tool_execution(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    task_id = UUID(str(domain["task"]["id"]))

    class CancellingProvider:
        name = "mock"
        paid = False

        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls == 2:
                async with get_session_factory()() as other:
                    await TaskStateMachine(other).transition(
                        task_id, TaskStatus.CANCELLED, "cancel during model turn"
                    )
            return ModelResponse(
                output={
                    "type": "tool_call",
                    "tool_name": "filesystem.list",
                    "arguments": {"path": "."},
                },
                provider="mock",
                model=request.model,
            )

    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=CancellingProvider()).execute(
                task_id
            )
    assert raised.value.code == "AGENT_EXECUTION_CANCELLED"

    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        runs = list(await session.scalars(select(AgentRun).order_by(AgentRun.created_at)))
        assert task is not None and task.status == TaskStatus.CANCELLED
        assert [run.status for run in runs] == [
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.CANCELLED,
        ]
        assert await session.scalar(select(func.count()).select_from(ToolCall)) == 1


async def test_same_tool_call_is_not_executed_twice_concurrently(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    task_id = UUID(str(domain["task"]["id"]))
    await transition_task(client, str(task_id), "IN_PROGRESS")
    now = datetime.now(UTC)
    execution_count = 0

    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        value: str

    async def side_effect(arguments: BaseModel, context: ToolExecutionContext) -> BaseModel:
        nonlocal execution_count
        del context
        execution_count += 1
        await asyncio.sleep(0.05)
        return Output(value=Input.model_validate(arguments).value)

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="filesystem.write",
            description="Concurrency test",
            input_model=Input,
            output_model=Output,
            risk_level=ToolRiskLevel.MEDIUM,
            permission_required="filesystem.write",
            timeout_seconds=1,
            enabled=True,
            handler=side_effect,
        )
    )
    async with get_session_factory()() as session:
        task_run = await session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert task_run is not None
        agent_run = AgentRun(
            task_run_id=task_run.id,
            task_id=task_id,
            agent_id=task_run.agent_id,
            status=AgentRunStatus.SUCCEEDED,
            provider="mock",
            model_alias="default",
            model_id="mock",
            request={},
            response={"type": "tool_call"},
            started_at=now,
            completed_at=now,
        )
        session.add(agent_run)
        await session.flush()
        call = ToolCall(
            agent_run_id=agent_run.id,
            task_run_id=task_run.id,
            task_id=task_id,
            agent_id=task_run.agent_id,
            tool_name="filesystem.write",
            status=ToolCallStatus.AUTHORIZED,
            arguments={"value": "once"},
            permission="filesystem.write",
        )
        session.add(call)
        await session.commit()
        call_id = call.id
        context = ToolExecutionContext(
            company_id=UUID(str(domain["company"]["id"])),
            project_id=UUID(str(domain["project"]["id"])),
            task_id=task_id,
            task_run_id=task_run.id,
            agent_id=task_run.agent_id,
            agent_run_id=agent_run.id,
        )

    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))

    async def execute_once() -> str:
        async with get_session_factory()() as session:
            result = await ToolExecutionService(
                session, settings=settings, registry=registry
            ).execute_existing(call_id, context)
            return result.status

    results = await asyncio.gather(execute_once(), execute_once())
    assert execution_count == 1
    assert sorted(results) == ["error", "success"]

    async with get_session_factory()() as session:
        call = await session.get(ToolCall, call_id)
        assert call is not None and call.status == ToolCallStatus.SUCCEEDED


async def test_stale_tool_call_recovery_reconciles_task(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_tool_domain(client)
    task_id = UUID(str(domain["task"]["id"]))
    await transition_task(client, str(task_id), "IN_PROGRESS")
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        task_run = await session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert task_run is not None
        agent_run = AgentRun(
            task_run_id=task_run.id,
            task_id=task_id,
            agent_id=task_run.agent_id,
            status=AgentRunStatus.SUCCEEDED,
            provider="mock",
            model_alias="default",
            model_id="mock",
            request={},
            response={"type": "tool_call"},
            started_at=now,
            completed_at=now,
        )
        session.add(agent_run)
        await session.flush()
        session.add(
            ToolCall(
                agent_run_id=agent_run.id,
                task_run_id=task_run.id,
                task_id=task_id,
                agent_id=task_run.agent_id,
                tool_name="filesystem.write",
                status=ToolCallStatus.RUNNING,
                arguments={"path": "ambiguous.txt", "content": "maybe"},
                permission="filesystem.write",
                started_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"), tool_call_stale_seconds=1)
    async with get_session_factory()() as session:
        assert await StaleToolCallRecovery(session, settings).recover() == 1

    async with get_session_factory()() as session:
        call = await session.scalar(select(ToolCall))
        task = await session.get(Task, task_id)
        assert call is not None and call.status == ToolCallStatus.FAILED
        assert call.error["code"] == "TOOL_EXECUTION_INTERRUPTED"
        assert task is not None and task.status == TaskStatus.FAILED
