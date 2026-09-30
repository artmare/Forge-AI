import asyncio
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent_runtime.contracts import ModelResponse, ModelUsage
from app.agent_runtime.efficiency import EfficientRuntimeService
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.recovery_evidence import RecoveryEvidenceService
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.tools import DevelopmentOutput, EmptyInput
from app.domain.enums import (
    DevelopmentAction,
    DevelopmentExecutionStatus,
    ToolPermission,
    ToolRiskLevel,
)
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import AgentRun, Event, Task, ToolCall
from app.infrastructure.database import get_session_factory
from app.services.event_factory import EventFactory
from app.tool_system.contracts import ToolDefinition
from app.tool_system.registry import ToolRegistry
from tests.helpers import create_agent, create_company, create_project, transition_task


def _git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(path: str, content: str) -> dict:
    return {
        "type": "tool_call",
        "tool_name": "filesystem.write",
        "arguments": {"path": path, "content": content},
    }


def _final(*, scope: str, calls: list[ToolCall]) -> dict:
    return {
        "type": "final",
        "result": {
            "status": "completed",
            "summary": "The previous execution created the NovaNote artifacts.",
            "output": {
                "artifacts": [str(call.result["path"]) for call in calls],
                "details": ["Current Forge verification confirms the checkpoint artifacts."],
                "execution_claims": [
                    {
                        "kind": "FILE_MUTATION",
                        "reference": str(call.result["path"]),
                        "scope": scope,
                        "tool_call_id": str(call.id),
                    }
                    for call in calls
                ],
            },
            "notes": [],
        },
    }


def _git_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()

    def definition(tool_name: str, action: DevelopmentAction) -> ToolDefinition:
        async def execute(_payload: EmptyInput, _context) -> DevelopmentOutput:
            arguments = (
                ("status", "--short")
                if action == DevelopmentAction.GIT_STATUS
                else ("diff", "--no-ext-diff", "--no-textconv", "--")
            )
            completed = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(workspace), *arguments],
                check=False,
                capture_output=True,
                text=True,
            )
            status = (
                DevelopmentExecutionStatus.SUCCEEDED
                if completed.returncode == 0
                else DevelopmentExecutionStatus.FAILED
            )
            return DevelopmentOutput(
                execution_id=str(uuid4()),
                action=action,
                status=status,
                exit_code=completed.returncode,
                stdout_excerpt=completed.stdout,
                stderr_excerpt=completed.stderr,
                output_truncated=False,
                duration_ms=0,
                change_summary=None,
            )

        return ToolDefinition(
            tool_name,
            f"Inspect {action.value} in the isolated recovery fixture.",
            EmptyInput,
            DevelopmentOutput,
            ToolRiskLevel.LOW,
            ToolPermission.GIT_READ.value,
            10,
            True,
            execute,
        )

    registry.register(definition("git.status", DevelopmentAction.GIT_STATUS))
    registry.register(definition("git.diff", DevelopmentAction.GIT_DIFF))
    return registry


class _RecoveryGitProvider:
    name = "mock"
    paid = False

    def __init__(self, historical_calls: list[ToolCall]) -> None:
        self.historical_calls = historical_calls
        self.call_count = 0
        self.requests = []

    async def generate(self, request) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 1:
            output = {"type": "tool_call", "tool_name": "git.status", "arguments": {}}
        elif self.call_count == 2:
            output = {"type": "tool_call", "tool_name": "git.diff", "arguments": {}}
        else:
            git_claims = [
                {
                    "kind": "GIT",
                    "reference": exchange.response["result"]["action"],
                    "scope": "CURRENT_RUN",
                    "tool_call_id": exchange.response["tool_call_id"],
                }
                for exchange in request.tool_exchanges
                if exchange.response.get("tool") in {"git.status", "git.diff"}
            ]
            output = _final(scope="HISTORICAL", calls=self.historical_calls)
            output["result"]["output"]["execution_claims"].extend(git_claims)
        return ModelResponse(
            output=output,
            usage=ModelUsage(input_tokens=12, output_tokens=18, total_tokens=30),
            provider=self.name,
            model=request.model,
            response_id=f"recovery-git-{self.call_count}",
        )


async def _recovery_fixture(client: AsyncClient, tmp_path: Path) -> dict:
    company = await create_company(client, slug=f"recovery-{uuid4().hex[:10]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.write": True, "git.read": True},
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Scaffold NovaNote static landing page",
            "input": {"deliverables": ["index.html", "styles.css", "app.js"]},
            "acceptance_criteria": ["The three static assets exist"],
            "max_iterations": 4,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(model_provider="mock", tool_workspace_root=str(tmp_path))
    workspace = tmp_path / company["id"] / project["id"]
    workspace.mkdir(parents=True)
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "config", "user.email", "forge@example.invalid")
    _git(workspace, "config", "user.name", "Forge")
    _git(workspace, "commit", "--allow-empty", "-m", "initial")

    provider = MockModelProvider(
        responses=[
            _write("index.html", "<main>NovaNote</main>\n"),
            _write("styles.css", "main { color: navy; }\n"),
            _write("app.js", "document.body.dataset.ready = 'true';\n"),
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "All tests passed.",
                    "output": {
                        "artifacts": ["index.html", "styles.css", "app.js"],
                        "details": [],
                        "execution_claims": [{"kind": "TEST", "reference": "NODE_TEST"}],
                    },
                    "notes": [],
                },
            },
        ]
    )
    task_id = UUID(task["id"])
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as failed:
            await AgentRuntime(session, settings=settings, provider=provider).execute(task_id)
        assert failed.value.code == "UNVERIFIED_EXECUTION_CLAIM"
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == task_id, ToolCall.tool_name == "filesystem.write")
                .order_by(ToolCall.created_at)
            )
        )
        assert len(calls) == 3

    _git(workspace, "add", "index.html", "styles.css", "app.js")
    _git(workspace, "commit", "-m", f"forge(task:{task_id}): development checkpoint")
    checkpoint = _git(workspace, "rev-parse", "--short", "HEAD")
    checkpoint_text = f"[main {checkpoint}] forge(task:{task_id}): development checkpoint"
    async with get_session_factory()() as session:
        model = await session.get(Task, task_id)
        assert model is not None
        await EventFactory(session).create(
            company_id=model.company_id,
            project_id=model.project_id,
            agent_id=model.assigned_agent_id,
            task_id=model.id,
            correlation_id=model.id,
            event_type="DEV_CONTEXT_FAILURE_HANDOFF",
            message="NovaNote recovery fixture handoff.",
            payload={
                "task_id": str(task_id),
                "stop_reason": "MODEL_CONTEXT_ROLLOVER_LIMIT_EXHAUSTED",
                "checkpoint": {"created": True, "checkpoint": checkpoint_text},
                "files_modified": ["index.html", "styles.css", "app.js"],
                "rollover_count": 4,
                "rollover_limit": 4,
            },
        )
        await session.commit()
        await EfficientRuntimeService(session, settings).approve_context_resume(task_id)
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == task_id, ToolCall.tool_name == "filesystem.write")
                .order_by(ToolCall.created_at)
            )
        )
    return {
        "task_id": task_id,
        "project_id": UUID(project["id"]),
        "settings": settings,
        "workspace": workspace,
        "calls": calls,
        "checkpoint": checkpoint,
    }


async def test_recovery_evidence_is_cross_run_provenanced_and_currently_revalidated(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _recovery_fixture(client, tmp_path)
    async with get_session_factory()() as session:
        task = await session.get(Task, fixture["task_id"])
        assert task is not None
        snapshot = await RecoveryEvidenceService(
            session, fixture["settings"]
        ).revalidate(task)

    assert [item.reference for item in snapshot.evidence] == [
        "app.js",
        "index.html",
        "styles.css",
    ]
    by_path = {str(call.result["path"]): call for call in fixture["calls"]}
    for item in snapshot.evidence:
        original = by_path[str(item.reference)]
        assert item.agent_run_id == original.agent_run_id
        assert item.tool_call_id == original.id
        assert item.task_id == fixture["task_id"]
        assert item.project_id == fixture["project_id"]
        assert item.checkpoint == fixture["checkpoint"]
        assert item.artifact_sha256


@pytest.mark.parametrize("change", ["modify", "delete"])
async def test_changed_or_deleted_recovery_artifact_invalidates_historical_evidence(
    client: AsyncClient, tmp_path: Path, change: str
) -> None:
    fixture = await _recovery_fixture(client, tmp_path)
    target = fixture["workspace"] / "app.js"
    target.write_text("changed\n") if change == "modify" else target.unlink()
    async with get_session_factory()() as session:
        task = await session.get(Task, fixture["task_id"])
        assert task is not None
        snapshot = await RecoveryEvidenceService(
            session, fixture["settings"]
        ).revalidate(task)

    assert "app.js" not in {item.reference for item in snapshot.evidence}
    assert {item["reason"] for item in snapshot.invalidated} == {
        "CURRENT_CONTENT_CHANGED" if change == "modify" else "MISSING_OR_UNSAFE"
    }


async def test_novanote_recovery_repairs_scope_without_replaying_mutations(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _recovery_fixture(client, tmp_path)
    invalid = _final(scope="CURRENT_RUN", calls=fixture["calls"])
    async with get_session_factory()() as session:
        provider = MockModelProvider(responses=[invalid, invalid])
        with pytest.raises(AgentRuntimeDomainError) as failed:
            await AgentRuntime(
                session, settings=fixture["settings"], provider=provider
            ).execute(fixture["task_id"])
        assert failed.value.code == "EXECUTION_TRUTH_REPAIR_LIMIT_EXHAUSTED"
        calls = list(
            await session.scalars(
                select(ToolCall).where(ToolCall.task_id == fixture["task_id"])
            )
        )
        rejected_runs = list(
            await session.scalars(
                select(AgentRun).where(
                    AgentRun.task_id == fixture["task_id"],
                    AgentRun.error_code == "UNVERIFIED_EXECUTION_CLAIM",
                )
            )
        )
        assert len(calls) == 3
        assert len(rejected_runs) >= 2
        assert all(run.response is not None and run.input_tokens > 0 for run in rejected_runs[-2:])

    approval = await client.post(f"/api/v1/tasks/{fixture['task_id']}/resume-recovery")
    assert approval.status_code == 200, approval.text
    assert approval.json()["max_iterations"] == 4
    duplicate = await client.post(f"/api/v1/tasks/{fixture['task_id']}/resume-recovery")
    assert duplicate.status_code == 409
    diagnostics = await client.get(f"/api/v1/tasks/{fixture['task_id']}/dev-mode")
    assert diagnostics.status_code == 200, diagnostics.text
    assert diagnostics.json()["recovery_evidence"]["truth_repair_attempts"] == 2
    assert len(diagnostics.json()["recovery_evidence"]["verified"]) == 3

    async with get_session_factory()() as session:
        correct = MockModelProvider(response=_final(scope="HISTORICAL", calls=fixture["calls"]))
        run = await AgentRuntime(
            session, settings=fixture["settings"], provider=correct
        ).execute(fixture["task_id"])
        calls = list(
            await session.scalars(
                select(ToolCall).where(ToolCall.task_id == fixture["task_id"])
            )
        )
        truth_approval = await session.scalar(
            select(Event).where(
                Event.task_id == fixture["task_id"],
                Event.type == "DEV_EXECUTION_TRUTH_RESUME_APPROVED",
            )
        )
        task = await session.get(Task, fixture["task_id"])
        assert task is not None
        completed_snapshot = await RecoveryEvidenceService(
            session, fixture["settings"]
        ).revalidate(task)

    assert run.status.value == "SUCCEEDED"
    assert len(calls) == 3
    assert truth_approval is not None
    assert truth_approval.details["limits_unchanged"] is True
    assert truth_approval.details["model_calls_consumed"] >= 6
    assert task.status.value == "REVIEW"
    assert completed_snapshot.active is False
    assert all(
        (fixture["workspace"] / name).exists()
        for name in ("index.html", "styles.css", "app.js")
    )


async def test_novanote_recovery_accepts_current_canonical_git_evidence_without_repair(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _recovery_fixture(client, tmp_path)
    provider = _RecoveryGitProvider(fixture["calls"])

    async with get_session_factory()() as session:
        run = await AgentRuntime(
            session,
            settings=fixture["settings"],
            provider=provider,
            tool_registry=_git_registry(fixture["workspace"]),
        ).execute(fixture["task_id"])
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == fixture["task_id"])
                .order_by(ToolCall.created_at)
            )
        )
        repairs = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == fixture["task_id"],
                    Event.type == "DEV_EXECUTION_TRUTH_REPAIR_REQUIRED",
                )
            )
        )

    assert run.status.value == "SUCCEEDED"
    assert provider.call_count == 3
    assert [call.tool_name for call in calls] == [
        "filesystem.write",
        "filesystem.write",
        "filesystem.write",
        "git.status",
        "git.diff",
    ]
    assert repairs == []
    assert all(
        (fixture["workspace"] / name).exists()
        for name in ("index.html", "styles.css", "app.js")
    )
