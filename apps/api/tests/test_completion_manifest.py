import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent_runtime.builders import ContextBuilder
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.completion_contracts import (
    ArtifactProvenance,
    CompletionReadiness,
    ManifestEvidenceStatus,
)
from app.development.completion_manifest import CompletionManifestService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    QADecision,
    TaskRunStatus,
    ToolCallStatus,
)
from app.domain.models import (
    AcceptanceVerification,
    DevelopmentExecution,
    QAResult,
    Task,
    TaskRun,
    ToolCall,
)
from app.infrastructure.database import get_session_factory
from app.services.event_factory import EventFactory
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import create_agent, create_company, create_project, transition_task


def _write(path: str, content: str) -> dict:
    return {
        "type": "tool_call",
        "tool_name": "filesystem.write",
        "arguments": {"path": path, "content": content},
    }


def _final(artifacts: list[str]) -> dict:
    return {
        "type": "final",
        "result": {
            "status": "completed",
            "summary": "The requested static site is ready for Forge verification.",
            "output": {"artifacts": artifacts, "details": [], "execution_claims": []},
            "notes": [],
        },
    }


async def _static_run(client: AsyncClient, tmp_path: Path) -> dict:
    company = await create_company(client, slug=f"manifest-{uuid4().hex[:10]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.write": True},
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Build deterministic static fixture",
            "input": {"deliverables": ["index.html", "styles.css", "app.js"]},
            "acceptance_criteria": [
                "`index.html` exists",
                "Automated tests pass",
                "The UI looks professional",
            ],
            "max_iterations": 4,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(model_provider="mock", tool_workspace_root=str(tmp_path / "workspaces"))
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(company["id"]), UUID(project["id"])
    )
    secret_marker = "OPENROUTER_API_KEY=never-in-manifest"
    provider = MockModelProvider(
        responses=[
            _write(
                "index.html",
                '<link rel="stylesheet" href="styles.css"><main>Fixture</main>'
                '<script src="app.js"></script>',
            ),
            _write("styles.css", f"/* {secret_marker} */\nmain {{ color: navy; }}"),
            _write("app.js", "document.body.dataset.ready = 'true';"),
            _final(["index.html", "styles.css", "app.js"]),
        ]
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(task["id"]), defer_review=True
        )
    return {
        "task": task,
        "company": company,
        "project": project,
        "agent": agent,
        "settings": settings,
        "workspace": workspace,
        "run": run,
        "provider": provider,
        "secret_marker": secret_marker,
    }


async def test_manifest_derives_bounded_artifacts_and_static_web_evidence(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await _static_run(client, tmp_path)
    async with get_session_factory()() as session:
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            UUID(fixture["task"]["id"]), fixture["run"].task_run_id
        )
        request = await AgentRuntime(
            session, settings=fixture["settings"], provider=fixture["provider"]
        )._build_request(
            await ContextBuilder(session).build(UUID(fixture["task"]["id"])),
            "default",
            "mock",
            [],
            turn_number=5,
            budget_warning=False,
            duplicate_warning=False,
            force_final=True,
        )

    assert manifest.readiness == CompletionReadiness.BLOCKED
    assert any("deterministic evidence" in item for item in manifest.blocking_reasons)
    assert fixture["run"].response["status"] == "completed"
    assert {item.path for item in manifest.artifacts} == {"index.html", "styles.css", "app.js"}
    assert all(item.status == ManifestEvidenceStatus.VERIFIED for item in manifest.artifacts)
    assert all(
        item.provenance == ArtifactProvenance.CURRENT_TASK_RUN for item in manifest.artifacts
    )
    assert all(item.originating_tool_call_id is not None for item in manifest.artifacts)
    static = next(item for item in manifest.verification_results if item.kind == "STATIC_WEB")
    assert static.status == ManifestEvidenceStatus.VERIFIED
    payload = json.dumps(manifest.bounded_summary())
    assert fixture["secret_marker"] not in payload
    assert "document.body.dataset" not in payload
    assert CompletionManifestService.serialized_size(manifest) < 50_000
    assert fixture["provider"].call_count == 4
    assert int(request.metadata["completion_manifest_bytes"]) > 0
    assert "Forge-owned completion evidence" in request.user_prompt

    import app.api.routes.efficient_runtime as efficient_runtime_route
    import app.development.completion_manifest as completion_manifest_module

    monkeypatch.setattr(completion_manifest_module, "get_settings", lambda: fixture["settings"])
    monkeypatch.setattr(efficient_runtime_route, "get_settings", lambda: fixture["settings"])
    response = await client.get(f"/api/v1/tasks/{fixture['task']['id']}/dev-mode")
    assert response.status_code == 200, response.text
    diagnostic = response.json()["completion_manifest"]
    assert diagnostic["readiness"] == "BLOCKED"
    assert diagnostic["manifest_bytes"] < 50_000
    assert diagnostic["model_visible_bytes"] < diagnostic["manifest_bytes"]


async def test_changed_deleted_and_missing_artifacts_block_readiness(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_run(client, tmp_path)
    workspace: Path = fixture["workspace"]
    (workspace / "styles.css").write_text("changed", encoding="utf-8")
    (workspace / "app.js").unlink()
    async with get_session_factory()() as session:
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            UUID(fixture["task"]["id"]), fixture["run"].task_run_id
        )
    by_path = {item.path: item for item in manifest.artifacts}
    assert by_path["styles.css"].status == ManifestEvidenceStatus.INVALIDATED
    assert by_path["app.js"].status == ManifestEvidenceStatus.MISSING
    assert manifest.readiness == CompletionReadiness.BLOCKED
    assert any(
        item.status == ManifestEvidenceStatus.FAILED
        for item in manifest.verification_results
    )


async def test_missing_statically_referenced_asset_is_deterministically_failed(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_run(client, tmp_path)
    index = fixture["workspace"] / "index.html"
    index.write_text('<script src="missing.js"></script>', encoding="utf-8")
    async with get_session_factory()() as session:
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            UUID(fixture["task"]["id"]), fixture["run"].task_run_id
        )
    static = next(item for item in manifest.verification_results if item.kind == "STATIC_WEB")
    assert static.status == ManifestEvidenceStatus.FAILED
    assert "missing.js" in static.summary
    assert manifest.readiness == CompletionReadiness.BLOCKED


async def test_normalized_verification_and_acceptance_drive_readiness(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_run(client, tmp_path)
    task_id = UUID(fixture["task"]["id"])
    run = fixture["run"]
    async with get_session_factory()() as session:
        executions = []
        for action in (
            DevelopmentAction.NODE_TEST,
            DevelopmentAction.NODE_BUILD,
            DevelopmentAction.NODE_LINT,
            DevelopmentAction.NODE_TYPECHECK,
        ):
            execution = DevelopmentExecution(
                company_id=UUID(fixture["company"]["id"]),
                project_id=UUID(fixture["project"]["id"]),
                task_id=task_id,
                task_run_id=run.task_run_id,
                agent_run_id=run.id,
                agent_id=UUID(fixture["agent"]["id"]),
                action=action,
                status=DevelopmentExecutionStatus.SUCCEEDED,
                working_directory=str(fixture["workspace"]),
                safe_arguments={},
                execution_origin="FORGE_QA",
                exit_code=0,
                stdout_excerpt=f"{action.value} passed",
                stderr_excerpt="",
                stdout_bytes=len(action.value) + 7,
                stderr_bytes=0,
                output_truncated=False,
                network_enabled=False,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                duration_ms=1,
                timeout_seconds=60,
                correlation_id=task_id,
            )
            executions.append(execution)
        session.add_all(executions)
        session.add_all(
            [
                AcceptanceVerification(
                    task_id=task_id,
                    task_run_id=run.task_run_id,
                    criterion_index=0,
                    criterion="`index.html` exists",
                    iteration=1,
                    status=AcceptanceVerificationStatus.PASSED,
                    verifier="FORGE_DETERMINISTIC_QA",
                    verifier_agent_id=UUID(fixture["agent"]["id"]),
                    evidence_summary="Current file exists.",
                    execution_ids=[],
                ),
                AcceptanceVerification(
                    task_id=task_id,
                    task_run_id=run.task_run_id,
                    criterion_index=1,
                    criterion="Automated tests pass",
                    iteration=1,
                    status=AcceptanceVerificationStatus.PASSED,
                    verifier="FORGE_DETERMINISTIC_QA",
                    verifier_agent_id=UUID(fixture["agent"]["id"]),
                    evidence_summary="NODE_TEST exited 0.",
                    execution_ids=[],
                ),
                AcceptanceVerification(
                    task_id=task_id,
                    task_run_id=run.task_run_id,
                    criterion_index=2,
                    criterion="The UI looks professional",
                    iteration=1,
                    status=AcceptanceVerificationStatus.UNVERIFIED,
                    verifier="FORGE_PRODUCT_QA",
                    verifier_agent_id=UUID(fixture["agent"]["id"]),
                    evidence_summary="Requires rendered visual judgment.",
                    execution_ids=[],
                ),
            ]
        )
        session.add(
            QAResult(
                task_id=task_id,
                task_run_id=run.task_run_id,
                verifier_agent_id=UUID(fixture["agent"]["id"]),
                iteration=1,
                decision=QADecision.PASS,
                summary="Deterministic gates passed.",
                checks=[],
                blocking_issues=[],
                non_blocking_issues=[],
                correlation_id=task_id,
            )
        )
        await session.commit()
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            task_id, run.task_run_id
        )
        test_evidence = next(
            item for item in manifest.verification_results if item.reference == "NODE_TEST"
        )
        assert test_evidence.status == ManifestEvidenceStatus.VERIFIED
        assert test_evidence.execution_id is not None and test_evidence.exit_code == 0
        assert {
            "NODE_TEST",
            "NODE_BUILD",
            "NODE_LINT",
            "NODE_TYPECHECK",
        }.issubset({item.reference for item in manifest.verification_results})
        assert manifest.readiness == CompletionReadiness.REQUIRES_JUDGMENT
        assert manifest.judgment_required == ["The UI looks professional"]

        lint_row = next(item for item in executions if item.action == DevelopmentAction.NODE_LINT)
        lint_row.status = DevelopmentExecutionStatus.FAILED
        lint_row.exit_code = 1
        await session.commit()
        blocked = await CompletionManifestService(session, fixture["settings"]).build(
            task_id, run.task_run_id
        )
        assert blocked.readiness == CompletionReadiness.BLOCKED


async def test_browser_evidence_is_bounded_and_does_not_assert_visual_quality(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_run(client, tmp_path)
    async with get_session_factory()() as session:
        call = ToolCall(
            agent_run_id=fixture["run"].id,
            task_run_id=fixture["run"].task_run_id,
            task_id=UUID(fixture["task"]["id"]),
            agent_id=UUID(fixture["agent"]["id"]),
            tool_name="browser.capture",
            status=ToolCallStatus.SUCCEEDED,
            arguments={"path": "index.html", "width": 1280, "height": 900},
            result={
                "path": "index.html",
                "artifact": f"browser/{uuid4()}.png",
                "sha256": "a" * 64,
                "rendered": True,
                "title": "Fixture",
                "console_errors": [],
                "viewport": {"width": 1280, "height": 900},
            },
            permission="browser.capture",
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        session.add(call)
        await session.commit()
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            UUID(fixture["task"]["id"]), fixture["run"].task_run_id
        )
        browser = next(item for item in manifest.verification_results if item.kind == "BROWSER")
        assert browser.status == ManifestEvidenceStatus.VERIFIED
        assert browser.tool_call_id == call.id
        assert "looks professional" not in browser.summary.lower()

        call.result = {**call.result, "console_errors": ["Uncaught fixture error"]}
        await session.commit()
        failed = await CompletionManifestService(session, fixture["settings"]).build(
            UUID(fixture["task"]["id"]), fixture["run"].task_run_id
        )
    failed_browser = next(item for item in failed.verification_results if item.kind == "BROWSER")
    assert failed_browser.status == ManifestEvidenceStatus.FAILED


async def test_approved_recovery_preserves_original_provenance_without_replay(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_run(client, tmp_path)
    task_id = UUID(fixture["task"]["id"])
    async with get_session_factory()() as session:
        original = await session.scalar(
            select(ToolCall).where(
                ToolCall.task_id == task_id,
                ToolCall.tool_name == "filesystem.write",
                ToolCall.result["path"].astext == "index.html",
            )
        )
        assert original is not None
        old_run = await session.get(TaskRun, fixture["run"].task_run_id)
        task = await session.get(Task, task_id)
        assert old_run is not None and task is not None
        old_run.status = TaskRunStatus.SUCCEEDED
        old_run.completed_at = datetime.now(UTC)
        task.iteration = 2
        current = TaskRun(
            task_id=task_id,
            agent_id=UUID(fixture["agent"]["id"]),
            iteration=2,
            status=TaskRunStatus.STARTED,
            input={},
        )
        session.add(current)
        await session.flush()
        await EventFactory(session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type="DEV_RECOVERY_EVIDENCE_VERIFIED",
            message="Historical artifacts revalidated.",
            payload={
                "checkpoint": "4acdf73",
                "verified": [
                    {
                        "path": "index.html",
                        "sha256": CompletionManifestService._sha256_file(
                            fixture["workspace"] / "index.html"
                        ),
                        "original_tool_call_id": str(original.id),
                    }
                ],
                "invalidated": [],
            },
        )
        await session.commit()
        before = len(
            list(
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.task_id == task_id,
                        ToolCall.tool_name == "filesystem.write",
                    )
                )
            )
        )
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            task_id, current.id
        )
        after = len(
            list(
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.task_id == task_id,
                        ToolCall.tool_name == "filesystem.write",
                    )
                )
            )
        )
    index = next(item for item in manifest.artifacts if item.path == "index.html")
    styles = next(item for item in manifest.artifacts if item.path == "styles.css")
    assert index.provenance == ArtifactProvenance.RECOVERY_HISTORY
    assert index.originating_tool_call_id == original.id
    assert index.originating_task_run_id == fixture["run"].task_run_id
    assert index.checkpoint == "4acdf73" and index.checkpoint_present is True
    assert styles.provenance == ArtifactProvenance.TASK_HISTORY
    assert styles.status == ManifestEvidenceStatus.VERIFIED
    assert styles.originating_task_run_id == fixture["run"].task_run_id
    assert before == after == 3
