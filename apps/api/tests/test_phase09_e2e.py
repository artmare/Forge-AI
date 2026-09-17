import asyncio
import json
import os
import shutil
import sys
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    QADecision,
    TaskStatus,
)
from app.domain.models import (
    AcceptanceVerification,
    AgentRun,
    DevelopmentExecution,
    Event,
    ProjectDevelopmentProfile,
    QAResult,
    Task,
    TaskRun,
    ToolCall,
)
from app.infrastructure.database import get_session_factory
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import create_agent, create_company, create_project, transition_task


def tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"type": "tool_call", "tool_name": tool_name, "arguments": arguments}


def write(path: str, content: str) -> dict[str, object]:
    return tool("filesystem.write", {"path": path, "content": content})


@pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="isolated runner volumes are required for the Phase 09 E2E",
)
async def test_clipmind_mock_developer_to_qa_to_human_review(
    client: AsyncClient,
    request: pytest.FixtureRequest,
) -> None:
    settings = Settings(
        model_provider="mock",
        allow_paid_model_calls=False,
        tool_workspace_root="/workspaces",
        development_runner_queue_root="/runner-queue",
        development_runner_mode="queue",
    )
    company = await create_company(client, slug=f"clipmind-e2e-{uuid4().hex[:8]}")
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
    task_response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Create ClipMind Manifest V3 selection-capture skeleton",
            "description": "Build the first bounded ClipMind backlog slice.",
            "acceptance_criteria": [
                "`manifest.json` exists",
                "npm test exits 0",
                "npm build exits 0",
            ],
            "max_iterations": 3,
        },
    )
    assert task_response.status_code == 201, task_response.text
    task_payload = task_response.json()
    await transition_task(client, str(task_payload["id"]), "QUEUED")

    workspace_manager = WorkspaceManager(settings.tool_workspace_root)
    workspace = workspace_manager.project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    request.addfinalizer(lambda: shutil.rmtree(workspace.parent, ignore_errors=True))
    planning_documents = {
        "market-analysis.md": "# ClipMind market\nUsers need frictionless selected-text capture.",
        "product-spec.md": "# Product spec\nManifest V3 extension with a Side Panel.",
        "ux-spec.md": "# UX spec\nDisplay the captured selection clearly and safely.",
        "technical-plan.md": "# Technical plan\nUse pure modules and deterministic Node tests.",
        "mvp-backlog.md": "# MVP backlog\nSkeleton, selection capture, Side Panel, tests.",
        "README.md": "# ClipMind\nPlanning artifacts approved for development validation.",
    }
    for path, content in planning_documents.items():
        WorkspaceManager.atomic_write(workspace / path, content.encode())

    sibling_company = uuid4()
    sibling_project = uuid4()
    sibling_workspace = workspace_manager.project_workspace(sibling_company, sibling_project)
    request.addfinalizer(lambda: shutil.rmtree(sibling_workspace.parent, ignore_errors=True))
    WorkspaceManager.atomic_write(sibling_workspace / "secret.txt", b"sibling-project-secret")
    sibling_secret = f"/workspaces/{sibling_company}/{sibling_project}/secret.txt"

    manifest = json.dumps(
        {
            "manifest_version": 3,
            "name": "ClipMind",
            "version": "0.1.0",
            "permissions": ["sidePanel", "contextMenus", "storage"],
            "background": {"service_worker": "src/capture.mjs", "type": "module"},
            "side_panel": {"default_path": "sidepanel.html"},
        },
        indent=2,
    )
    package = json.dumps(
        {
            "name": "clipmind-extension",
            "version": "0.1.0",
            "private": True,
            "type": "module",
            "scripts": {
                "test": "node --test tests/selection.test.mjs",
                "build": "node scripts/build.mjs",
            },
        },
        indent=2,
    )
    capture = """export function normalizeSelection(value) {
  return String(value ?? "").trim().replace(/\\s+/g, " ");
}

if (globalThis.chrome?.runtime?.onInstalled) {
  chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({
      id: "clipmind-capture",
      title: "Save to ClipMind",
      contexts: ["selection"],
    });
  });
  chrome.contextMenus.onClicked.addListener(async (info) => {
    if (info.menuItemId === "clipmind-capture") {
      await chrome.storage.local.set({ selectedText: normalizeSelection(info.selectionText) });
      await chrome.sidePanel.open({ tabId: info.tabId });
    }
  });
}
"""
    broken_capture = capture.replace('.replace(/\\s+/g, " ")', "")
    sidepanel = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>ClipMind</title>
<style>
body { font: 16px system-ui; margin: 24px; background: #111827; color: #f9fafb; }
output { display: block; white-space: pre-wrap; }
</style>
<main>
  <h1>ClipMind</h1><p>Captured selection</p>
  <output id="selection">Nothing captured yet.</output>
</main>
<script type="module">
const data = await chrome.storage.local.get("selectedText");
document.querySelector("#selection").textContent = data.selectedText || "Nothing captured yet.";
</script>
</html>
"""
    selection_test = f"""import assert from "node:assert/strict";
import {{ readFileSync }} from "node:fs";
import test from "node:test";
import {{ normalizeSelection }} from "../src/capture.mjs";

test("normalizes selected text", () => {{
  assert.equal(normalizeSelection("  Forge   builds\\nsoftware "), "Forge builds software");
}});

test("isolated runner exposes no Forge secrets or sibling workspace", () => {{
  assert.equal(process.env.OPENAI_API_KEY, undefined);
  assert.equal(process.env.DATABASE_URL, undefined);
  assert.equal(process.env.REDIS_URL, undefined);
  assert.throws(() => readFileSync("{sibling_secret}", "utf8"));
  assert.throws(() => readFileSync("/runner-queue/offline/requests", "utf8"));
  assert.throws(() => readFileSync("/var/run/docker.sock", "utf8"));
}});
"""
    build_script = """import { access, readFile } from "node:fs/promises";
const required = ["manifest.json", "sidepanel.html", "src/capture.mjs"];
for (const path of required) await access(path);
const manifest = JSON.parse(await readFile("manifest.json", "utf8"));
if (manifest.manifest_version !== 3) throw new Error("Manifest V3 is required");
console.log(`ClipMind build validated: ${required.length} runtime files`);
"""

    responses: list[dict[str, object]] = [
        *[tool("filesystem.read", {"path": path}) for path in planning_documents],
        write("manifest.json", manifest),
        write("package.json", package),
        write("src/capture.mjs", broken_capture),
        write("sidepanel.html", sidepanel),
        write("tests/selection.test.mjs", selection_test),
        write("scripts/build.mjs", build_script),
        tool("development.execute", {"action": "NODE_TEST"}),
        write("src/capture.mjs", capture),
        tool("development.execute", {"action": "NODE_TEST"}),
        tool("development.execute", {"action": "NODE_BUILD"}),
        tool("git.status", {}),
        tool("git.diff", {}),
        tool("git.commit", {}),
        {
            "type": "final",
            "result": {
                "status": "completed",
                "summary": "ClipMind skeleton implemented and validated with allowlisted actions.",
                "output": {
                    "artifacts": [
                        "manifest.json",
                        "package.json",
                        "src/capture.mjs",
                        "sidepanel.html",
                        "tests/selection.test.mjs",
                        "scripts/build.mjs",
                    ],
                    "details": ["NODE_TEST and NODE_BUILD exited successfully."],
                },
                "notes": ["No paid provider or unrestricted shell was used."],
            },
        },
    ]
    provider = MockModelProvider(responses=responses)
    async with get_session_factory()() as session:
        before = await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(str(task_payload["id"])), checkpoint=True
        )
        assert before.repository_initialized is True
        assert before.project_type.value == "UNKNOWN"
        developer_run = await AgentRuntime(
            session,
            settings=settings,
            provider=provider,
        ).execute(UUID(str(task_payload["id"])), defer_review=True)
        qa_result = await DevelopmentWorkflowService(session, settings=settings).finalize(
            UUID(str(task_payload["id"])), developer_run.task_run_id
        )
        task = await session.get(Task, task_payload["id"])
        profile = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        executions = list(
            await session.scalars(
                select(DevelopmentExecution)
                .where(DevelopmentExecution.task_id == task_payload["id"])
                .order_by(DevelopmentExecution.created_at)
            )
        )
        verifications = list(
            await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == task_payload["id"]
                )
            )
        )
        read_paths = set(
            await session.scalars(
                select(ToolCall.arguments["path"].as_string()).where(
                    ToolCall.task_id == task_payload["id"],
                    ToolCall.tool_name == "filesystem.read",
                )
            )
        )

    assert provider.paid is False
    assert provider.call_count == len(responses)
    assert set(planning_documents) <= read_paths
    assert qa_result.decision == QADecision.PASS, {
        "summary": qa_result.summary,
        "checks": qa_result.checks,
        "blocking": qa_result.blocking_issues,
        "executions": [
            (item.action.value, item.status.value, item.error_code, item.stderr_excerpt)
            for item in executions
        ],
    }
    assert task is not None and task.status == TaskStatus.REVIEW
    assert profile is not None
    assert profile.project_type.value == "NODE"
    assert profile.package_manager.value == "NPM"
    assert profile.repository_initialized is True
    assert profile.initial_checkpoint_created is True
    assert profile.test_action == DevelopmentAction.NODE_TEST
    assert profile.build_action == DevelopmentAction.NODE_BUILD
    assert all(item.status == AcceptanceVerificationStatus.PASSED for item in verifications)
    assert {item.action for item in executions} >= {
        DevelopmentAction.NODE_TEST,
        DevelopmentAction.NODE_BUILD,
        DevelopmentAction.GIT_STATUS,
        DevelopmentAction.GIT_DIFF,
        DevelopmentAction.GIT_CHECKPOINT,
    }
    developer_test_executions = [
        item
        for item in executions
        if item.action == DevelopmentAction.NODE_TEST
        and item.agent_id == UUID(str(developer["id"]))
    ]
    assert [item.status for item in developer_test_executions] == [
        DevelopmentExecutionStatus.FAILED,
        DevelopmentExecutionStatus.SUCCEEDED,
    ]
    assert '"status":"FAILED"' in provider.requests[13].user_prompt
    assert all(
        item.status == DevelopmentExecutionStatus.SUCCEEDED
        for item in executions
        if item not in developer_test_executions[:1]
    )
    assert any(
        item.action == DevelopmentAction.GIT_STATUS
        and (item.change_summary or {}).get("total", 0) >= 6
        for item in executions
    )
    assert not (workspace / ".env").exists()
    assert not (workspace / "node_modules").exists()
    expected_files = [
        *planning_documents,
        "manifest.json",
        "package.json",
        "src/capture.mjs",
        "sidepanel.html",
        "tests/selection.test.mjs",
        "scripts/build.mjs",
    ]
    assert sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file() and ".git/" not in path.relative_to(workspace).as_posix()
    ) == sorted(expected_files)


@pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="isolated runner volumes are required for the Phase 09 release E2E",
)
async def test_clean_slugify_qa_failure_fix_iteration_and_durable_review(
    client: AsyncClient,
    request: pytest.FixtureRequest,
) -> None:
    settings = Settings(
        model_provider="mock",
        allow_paid_model_calls=False,
        tool_workspace_root="/workspaces",
        development_runner_queue_root="/runner-queue",
        development_runner_mode="queue",
        developer_max_steps=20,
        development_max_executions=8,
        product_qa_enabled=False,
    )
    company = await create_company(client, slug=f"slugify-e2e-{uuid4().hex[:8]}")
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
    await create_agent(
        client,
        str(company["id"]),
        role="QA",
        permissions={
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
            "title": "Implement and verify a deterministic slugify module",
            "acceptance_criteria": [
                "`package.json` exists",
                "`src/slugify.mjs` exists",
                "npm test exits 0",
                "npm build exits 0",
            ],
            "max_iterations": 3,
        },
    )
    assert created.status_code == 201, created.text
    task_payload = created.json()
    await transition_task(client, str(task_payload["id"]), "QUEUED")

    workspaces = WorkspaceManager(settings.tool_workspace_root)
    workspace = workspaces.project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    request.addfinalizer(lambda: shutil.rmtree(workspace.parent, ignore_errors=True))
    package = json.dumps(
        {
            "name": "forge-slugify-gate",
            "private": True,
            "type": "module",
            "scripts": {
                "test": "node --test tests/slugify.test.mjs",
                "build": "node scripts/build.mjs",
            },
        },
        indent=2,
    )
    broken_source = """export function slugify(value) {
  return String(value ?? "").trim().toLowerCase().replace(/\\s+/g, "-");
}
"""
    fixed_source = """export function slugify(value) {
  return String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}
"""
    tests = """import assert from "node:assert/strict";
import test from "node:test";
import { slugify } from "../src/slugify.mjs";

test("normalizes punctuation and whitespace", () => {
  assert.equal(slugify("  Hello,   Forge!  "), "hello-forge");
});

test("keeps a stable empty result", () => {
  assert.equal(slugify(" --- "), "");
});
"""
    build = """import { slugify } from "../src/slugify.mjs";
if (typeof slugify !== "function") throw new Error("slugify export is required");
console.log("slugify build contract passed");
"""

    first_provider = MockModelProvider(
        responses=[
            write("package.json", package),
            write("src/slugify.mjs", broken_source),
            write("tests/slugify.test.mjs", tests),
            write("scripts/build.mjs", build),
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Initial slugify implementation is ready for independent QA.",
                    "output": {"artifacts": ["src/slugify.mjs", "tests/slugify.test.mjs"]},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        bootstrapped = await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(str(task_payload["id"])), checkpoint=True
        )
        assert bootstrapped.repository_initialized is True
        first_run = await AgentRuntime(
            session, settings=settings, provider=first_provider
        ).execute(UUID(str(task_payload["id"])), defer_review=True)
        first_qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
            UUID(str(task_payload["id"])), first_run.task_run_id
        )
        first_task = await session.get(Task, task_payload["id"])
        assert first_qa.decision == QADecision.FAIL
        assert first_task is not None and first_task.status == TaskStatus.QUEUED

    second_provider = MockModelProvider(
        responses=[
            write("src/slugify.mjs", fixed_source),
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Applied QA feedback and corrected punctuation normalization.",
                    "output": {"artifacts": ["src/slugify.mjs"]},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        second_run = await AgentRuntime(
            session, settings=settings, provider=second_provider
        ).execute(UUID(str(task_payload["id"])), defer_review=True)
        second_qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
            UUID(str(task_payload["id"])), second_run.task_run_id
        )
        assert second_qa.decision == QADecision.PASS
        task = await session.get(Task, task_payload["id"])
        profile = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        task_runs = list(
            await session.scalars(
                select(TaskRun).where(TaskRun.task_id == task_payload["id"])
            )
        )
        agent_runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.task_id == task_payload["id"])
            )
        )
        tool_calls = list(
            await session.scalars(
                select(ToolCall).where(ToolCall.task_id == task_payload["id"])
            )
        )
        executions = list(
            await session.scalars(
                select(DevelopmentExecution).where(
                    DevelopmentExecution.task_id == task_payload["id"]
                )
            )
        )
        qa_results = list(
            await session.scalars(
                select(QAResult)
                .where(QAResult.task_id == task_payload["id"])
                .order_by(QAResult.iteration)
            )
        )
        evidence = list(
            await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == task_payload["id"]
                )
            )
        )

    assert first_provider.call_count == 5
    assert second_provider.call_count == 2
    assert len(tool_calls) == 5 < settings.developer_max_steps
    assert len(executions) == 6
    assert all(
        sum(item.task_run_id == task_run.id for item in executions)
        <= settings.development_max_executions
        for task_run in task_runs
    )
    assert [item.decision for item in qa_results] == [QADecision.FAIL, QADecision.PASS]
    assert any(
        item.action == DevelopmentAction.NODE_TEST
        and item.status == DevelopmentExecutionStatus.FAILED
        for item in executions
    )
    assert all(
        item.status == AcceptanceVerificationStatus.PASSED
        for item in evidence
        if item.iteration == 2
    )
    assert len(task_runs) == 2
    assert len(agent_runs) == 9  # 7 Developer model turns + 2 deterministic QA runs.
    assert profile is not None and profile.initial_checkpoint_created is True
    assert profile.project_type.value == "NODE"
    assert task is not None and task.iteration == 2 and task.status == TaskStatus.REVIEW


@pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="isolated runner volumes are required for the Phase 09 failure gate",
)
async def test_impossible_development_task_stops_after_bounded_qa_iterations(
    client: AsyncClient,
    request: pytest.FixtureRequest,
) -> None:
    settings = Settings(
        model_provider="mock",
        allow_paid_model_calls=False,
        tool_workspace_root="/workspaces",
        development_runner_queue_root="/runner-queue",
        development_runner_mode="queue",
        developer_max_steps=20,
        development_max_executions=8,
        product_qa_enabled=False,
    )
    company = await create_company(client, slug=f"impossible-e2e-{uuid4().hex[:8]}")
    project = await create_project(client, str(company["id"]))
    developer = await create_agent(
        client,
        str(company["id"]),
        role="DEVELOPER",
        permissions={
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
        },
    )
    await create_agent(
        client,
        str(company["id"]),
        role="QA",
        permissions={"development.execute": True, "git.read": True},
    )
    created = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Prove an intentionally impossible implementation contract",
            "acceptance_criteria": ["npm test exits 0"],
            "max_iterations": 2,
        },
    )
    assert created.status_code == 201, created.text
    task_payload = created.json()
    await transition_task(client, str(task_payload["id"]), "QUEUED")
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    request.addfinalizer(lambda: shutil.rmtree(workspace.parent, ignore_errors=True))
    package = json.dumps(
        {
            "name": "forge-impossible-gate",
            "private": True,
            "type": "module",
            "scripts": {"test": "node --test tests/impossible.test.mjs"},
        }
    )
    failing_test = """import assert from "node:assert/strict";
import test from "node:test";
import { answer } from "../src/answer.mjs";
test("requires the impossible answer", () => assert.equal(answer(), 42));
"""
    attempts = [
        MockModelProvider(
            responses=[
                write("package.json", package),
                write("src/answer.mjs", "export const answer = () => 0;\n"),
                write("tests/impossible.test.mjs", failing_test),
                {
                    "type": "final",
                    "result": {
                        "status": "completed",
                        "summary": "First bounded implementation attempt completed.",
                        "output": {},
                        "notes": [],
                    },
                },
            ]
        ),
        MockModelProvider(
            responses=[
                write("src/answer.mjs", "export const answer = () => 41;\n"),
                {
                    "type": "final",
                    "result": {
                        "status": "completed",
                        "summary": "Second bounded implementation attempt completed.",
                        "output": {},
                        "notes": [],
                    },
                },
            ]
        ),
    ]
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(str(task_payload["id"])), checkpoint=True
        )

    decisions: list[QADecision] = []
    for provider in attempts:
        async with get_session_factory()() as session:
            run = await AgentRuntime(session, settings=settings, provider=provider).execute(
                UUID(str(task_payload["id"])), defer_review=True
            )
            qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
                UUID(str(task_payload["id"])), run.task_run_id
            )
            decisions.append(qa.decision)

    async with get_session_factory()() as session:
        task = await session.get(Task, task_payload["id"])
        task_runs = list(
            await session.scalars(
                select(TaskRun).where(TaskRun.task_id == task_payload["id"])
            )
        )
        agent_runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.task_id == task_payload["id"])
            )
        )
        qa_results = list(
            await session.scalars(
                select(QAResult).where(QAResult.task_id == task_payload["id"])
            )
        )
        executions = list(
            await session.scalars(
                select(DevelopmentExecution).where(
                    DevelopmentExecution.task_id == task_payload["id"]
                )
            )
        )

    assert decisions == [QADecision.FAIL, QADecision.FAIL]
    assert [provider.call_count for provider in attempts] == [4, 2]
    assert task is not None and task.status == TaskStatus.FAILED and task.iteration == 2
    assert len(task_runs) == 2
    assert len(qa_results) == 2
    assert all(result.failure_classification == "IMPLEMENTATION_FAILURE" for result in qa_results)
    assert all(run.status.value != "RUNNING" for run in agent_runs)
    assert len(executions) == 4


@pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="isolated runner volumes are required for the Phase 09 restart gate",
)
async def test_runtime_process_restart_at_safe_point_preserves_durable_execution(
    client: AsyncClient,
    request: pytest.FixtureRequest,
) -> None:
    company = await create_company(client, slug=f"restart-e2e-{uuid4().hex[:8]}")
    project = await create_project(client, str(company["id"]))
    developer = await create_agent(
        client,
        str(company["id"]),
        role="DEVELOPER",
        permissions={
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
        },
    )
    await create_agent(
        client,
        str(company["id"]),
        role="QA",
        permissions={"development.execute": True, "git.read": True},
    )
    created = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Verify durable state across a runtime process restart",
            "acceptance_criteria": ["npm test exits 0"],
            "max_iterations": 2,
        },
    )
    assert created.status_code == 201, created.text
    task_payload = created.json()
    task_id = str(task_payload["id"])
    await transition_task(client, task_id, "QUEUED")
    workspace = WorkspaceManager("/workspaces").project_workspace(
        UUID(str(company["id"])), UUID(str(project["id"]))
    )
    request.addfinalizer(lambda: shutil.rmtree(workspace.parent, ignore_errors=True))
    helper = "/app/tests/runtime_restart_helper.py"

    execute_process = await asyncio.create_subprocess_exec(
        sys.executable,
        helper,
        "execute",
        task_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    execute_stdout, execute_stderr = await asyncio.wait_for(
        execute_process.communicate(), timeout=60
    )
    assert execute_process.returncode == 0, execute_stderr.decode()
    execution_payload = json.loads(execute_stdout.decode().strip().splitlines()[-1])

    # The implementation process has exited. A fresh process resumes from only
    # PostgreSQL/workspace state and performs deterministic QA.
    finalize_process = await asyncio.create_subprocess_exec(
        sys.executable,
        helper,
        "finalize",
        task_id,
        execution_payload["task_run_id"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    finalize_stdout, finalize_stderr = await asyncio.wait_for(
        finalize_process.communicate(), timeout=60
    )
    assert finalize_process.returncode == 0, finalize_stderr.decode()
    final_payload = json.loads(finalize_stdout.decode().strip().splitlines()[-1])

    async with get_session_factory()() as session:
        task = await session.get(Task, UUID(task_id))
        profile = await session.scalar(
            select(ProjectDevelopmentProfile).where(
                ProjectDevelopmentProfile.project_id == UUID(str(project["id"]))
            )
        )
        task_runs = list(
            await session.scalars(select(TaskRun).where(TaskRun.task_id == UUID(task_id)))
        )
        agent_runs = list(
            await session.scalars(select(AgentRun).where(AgentRun.task_id == UUID(task_id)))
        )
        tool_calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task_id)))
        )
        evidence = list(
            await session.scalars(
                select(AcceptanceVerification).where(
                    AcceptanceVerification.task_id == UUID(task_id)
                )
            )
        )
        checkpoints = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == UUID(task_id),
                    Event.type == "DEVELOPMENT_INITIAL_CHECKPOINT_CREATED",
                )
            )
        )

    assert execution_payload == {
        "task_run_id": execution_payload["task_run_id"],
        "model_turns": 4,
    }
    assert final_payload == {"decision": "PASS", "iteration": 1}
    assert task is not None and task.status == TaskStatus.REVIEW and task.iteration == 1
    assert len(task_runs) == 1
    assert len(tool_calls) == 3
    assert len({call.arguments["path"] for call in tool_calls}) == 3
    assert all(run.status.value != "RUNNING" for run in agent_runs)
    assert len(agent_runs) == 5  # 4 Developer turns persisted before restart, then one QA run.
    assert len(evidence) == 1 and evidence[0].status == AcceptanceVerificationStatus.PASSED
    assert profile is not None and profile.initial_checkpoint_created is True
    assert len(checkpoints) == 1
