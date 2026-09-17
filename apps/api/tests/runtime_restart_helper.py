"""Run one durable development stage in a fresh process for the restart release gate."""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import UUID

from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.infrastructure.database import get_session_factory


def write(path: str, content: str) -> dict[str, object]:
    return {
        "type": "tool_call",
        "tool_name": "filesystem.write",
        "arguments": {"path": path, "content": content},
    }


def settings() -> Settings:
    return Settings(
        model_provider="mock",
        allow_paid_model_calls=False,
        tool_workspace_root="/workspaces",
        development_runner_queue_root="/runner-queue",
        development_runner_mode="queue",
        developer_max_steps=20,
        development_max_executions=8,
        product_qa_enabled=False,
    )


async def execute(task_id: UUID) -> None:
    package = json.dumps(
        {
            "name": "forge-restart-gate",
            "private": True,
            "type": "module",
            "scripts": {"test": "node --test tests/restart.test.mjs"},
        }
    )
    source = "export const persisted = () => 'durable';\n"
    test = """import assert from "node:assert/strict";
import test from "node:test";
import { persisted } from "../src/persisted.mjs";
test("survives a runtime restart", () => assert.equal(persisted(), "durable"));
"""
    provider = MockModelProvider(
        responses=[
            write("package.json", package),
            write("src/persisted.mjs", source),
            write("tests/restart.test.mjs", test),
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Durable implementation stage completed before restart.",
                    "output": {},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        configured = settings()
        await ProjectBootstrapService(session, settings=configured).ensure_task(
            task_id, checkpoint=True
        )
        run = await AgentRuntime(session, settings=configured, provider=provider).execute(
            task_id, defer_review=True
        )
        print(json.dumps({"task_run_id": str(run.task_run_id), "model_turns": provider.call_count}))


async def finalize(task_id: UUID, task_run_id: UUID) -> None:
    async with get_session_factory()() as session:
        result = await DevelopmentWorkflowService(session, settings=settings()).finalize(
            task_id, task_run_id
        )
        print(json.dumps({"decision": result.decision.value, "iteration": result.iteration}))


async def main() -> None:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "usage: runtime_restart_helper.py execute TASK_ID | finalize TASK_ID RUN_ID"
        )
    stage = sys.argv[1]
    task_id = UUID(sys.argv[2])
    if stage == "execute" and len(sys.argv) == 3:
        await execute(task_id)
        return
    if stage == "finalize" and len(sys.argv) == 4:
        await finalize(task_id, UUID(sys.argv[3]))
        return
    raise SystemExit("invalid restart stage")


if __name__ == "__main__":
    asyncio.run(main())
