"""Operator-only Phase 09 ClipMind smoke flow using the explicit mock provider."""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import select

from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import get_settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import QADecision, TaskKind, TaskStatus
from app.domain.models import Agent, Task
from app.infrastructure.database import close_database, get_session_factory
from app.schemas.agent import AgentCreate
from app.schemas.task import TaskCreate
from app.services.agent import AgentService
from app.services.task import TaskService
from app.services.task_state_machine import TaskStateMachine


def tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"type": "tool_call", "tool_name": name, "arguments": arguments}


def write(path: str, content: str) -> dict[str, object]:
    return tool("filesystem.write", {"path": path, "content": content})


def final(summary: str) -> dict[str, object]:
    return {
        "type": "final",
        "result": {
            "status": "completed",
            "summary": summary,
            "output": {
                "artifacts": [
                    "manifest.json",
                    "package.json",
                    "src/capture.mjs",
                    "sidepanel.html",
                    "tests/selection.test.mjs",
                    "scripts/build.mjs",
                ],
                "details": ["Allowlisted tests and build completed before independent QA."],
            },
            "notes": ["Explicit mock provider; no paid call or unrestricted shell."],
        },
    }


def initial_responses() -> list[dict[str, object]]:
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
    selection_test = """import assert from "node:assert/strict";
import test from "node:test";
import { normalizeSelection } from "../src/capture.mjs";

test("normalizes selected text", () => {
  assert.equal(normalizeSelection("  Forge   builds\\nsoftware "), "Forge builds software");
});
"""
    build_script = """import { access, readFile } from "node:fs/promises";
const required = ["manifest.json", "sidepanel.html", "src/capture.mjs"];
for (const path of required) await access(path);
const manifest = JSON.parse(await readFile("manifest.json", "utf8"));
if (manifest.manifest_version !== 3) throw new Error("Manifest V3 is required");
console.log(`ClipMind build validated: ${required.length} runtime files`);
"""
    return [
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
        final("ClipMind skeleton implemented after a bounded failing-test repair cycle."),
    ]


def revision_responses() -> list[dict[str, object]]:
    accessible_sidepanel = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>ClipMind</title>
<style>
body { font: 16px system-ui; margin: 24px; background: #111827; color: #f9fafb; }
output { display: block; white-space: pre-wrap; }
</style>
<main aria-labelledby="clipmind-title">
  <h1 id="clipmind-title">ClipMind</h1><p>Captured selection</p>
  <output id="selection" aria-live="polite">Nothing captured yet.</output>
</main>
<script type="module">
const data = await chrome.storage.local.get("selectedText");
document.querySelector("#selection").textContent = data.selectedText || "Nothing captured yet.";
</script>
</html>
"""
    return [
        tool("filesystem.read", {"path": "sidepanel.html"}),
        write("sidepanel.html", accessible_sidepanel),
        tool("development.execute", {"action": "NODE_TEST"}),
        tool("development.execute", {"action": "NODE_BUILD"}),
        tool("git.status", {}),
        tool("git.diff", {}),
        tool("git.commit", {}),
        final("Applied the persisted human accessibility feedback and revalidated ClipMind."),
    ]


async def get_or_create_agent(company_id: UUID, name: str, role: str) -> Agent:
    async with get_session_factory()() as session:
        existing = await session.scalar(
            select(Agent).where(Agent.company_id == company_id, Agent.name == name).limit(1)
        )
        if existing is not None:
            return existing
        permissions = (
            {
                "filesystem.list": True,
                "filesystem.read": True,
                "filesystem.write": True,
                "development.execute": True,
                "git.read": True,
                "git.write": True,
            }
            if role == "DEVELOPER"
            else {
                "filesystem.list": True,
                "filesystem.read": True,
                "development.execute": True,
                "git.read": True,
            }
        )
        return await AgentService(session).create(
            company_id,
            AgentCreate(
                name=name,
                role=role,
                configuration={"model_alias": "coding", "phase09_smoke": True},
                permissions=permissions,
            ),
        )


async def create_task(company_id: UUID, project_id: UUID, developer_id: UUID) -> Task:
    async with get_session_factory()() as session:
        task = await TaskService(session).create(
            TaskCreate(
                company_id=company_id,
                project_id=project_id,
                assigned_agent_id=developer_id,
                type="IMPLEMENTATION",
                kind=TaskKind.DEVELOPMENT,
                title="Phase 09 — ClipMind selection-capture skeleton",
                description=(
                    "Use the approved ClipMind planning artifacts to implement and validate the "
                    "first bounded Manifest V3 development slice."
                ),
                acceptance_criteria=[
                    "`manifest.json` exists",
                    "npm test exits 0",
                    "npm build exits 0",
                ],
                max_iterations=3,
            )
        )
        return await TaskStateMachine(session).transition(
            task.id, TaskStatus.QUEUED, "Operator started the explicit mock Phase 09 smoke flow."
        )


async def run(company_id: UUID, project_id: UUID, task_id: UUID | None) -> None:
    developer = await get_or_create_agent(company_id, "Phase 09 ClipMind Developer", "DEVELOPER")
    await get_or_create_agent(company_id, "Phase 09 ClipMind QA", "QA")
    task = (
        await create_task(company_id, project_id, developer.id)
        if task_id is None
        else await get_task(task_id)
    )
    if task.company_id != company_id or task.project_id != project_id:
        raise RuntimeError("Task does not belong to the requested ClipMind project")
    if task.status != TaskStatus.QUEUED:
        raise RuntimeError("Smoke execution requires a QUEUED development task")

    settings = get_settings().model_copy(
        update={"model_provider": "mock", "allow_paid_model_calls": False}
    )
    responses = initial_responses() if task_id is None else revision_responses()
    provider = MockModelProvider(responses=responses)
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings).ensure_task(
            task.id, checkpoint=True
        )
        developer_run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            task.id, defer_review=True
        )
        qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
            task.id, developer_run.task_run_id
        )
        stored = await session.get(Task, task.id)
        print(
            json.dumps(
                {
                    "task_id": str(task.id),
                    "task_run_id": str(developer_run.task_run_id),
                    "provider": provider.name,
                    "paid": provider.paid,
                    "model_calls": provider.call_count,
                    "qa_decision": qa.decision.value,
                    "qa_result_id": str(qa.id),
                    "task_status": stored.status.value if stored else None,
                },
                sort_keys=True,
            )
        )
        if provider.paid or qa.decision != QADecision.PASS or stored is None:
            raise RuntimeError("Phase 09 ClipMind smoke validation failed")


async def get_task(task_id: UUID) -> Task:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise RuntimeError("Task was not found")
        return task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=UUID, required=True)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--task-id", type=UUID)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    try:
        await run(args.company_id, args.project_id, args.task_id)
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
