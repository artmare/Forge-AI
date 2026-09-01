import json
import os
import shutil
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
    DevelopmentExecution,
    ProjectDevelopmentProfile,
    Task,
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
