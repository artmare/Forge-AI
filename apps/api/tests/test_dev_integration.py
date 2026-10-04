import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.agent_runtime.context_rollover import ContextRollover
from app.agent_runtime.contracts import ModelUsage
from app.agent_runtime.efficiency import EfficientRuntimeService
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.bootstrap import ProjectBootstrapService
from app.development.qa import DevelopmentWorkflowService
from app.domain.enums import TaskStatus
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import Event, ProjectKnowledgeIndex, TaskRuntimeMetric, ToolCall
from app.infrastructure.database import get_session_factory
from app.services.task_state_machine import TaskStateMachine
from tests.helpers import create_agent, create_company, create_project, create_task, transition_task


def final(summary="Completed", artifacts=None):
    return {
        "type": "final",
        "result": {
            "status": "completed",
            "summary": summary,
            "output": {"artifacts": artifacts or [], "details": []},
            "notes": [],
        },
    }


def tool(name, **arguments):
    return {"type": "tool_call", "tool_name": name, "arguments": arguments}


@pytest.mark.parametrize("replay", [False, True])
async def test_runtime_rollover_retains_truth_and_blocks_mutation_replay(client, tmp_path, replay):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client, company["id"], role="LEAD_ENGINEER", permissions={"filesystem.write": True}
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    write = tool("filesystem.write", path="result.txt", content="ok")
    provider = MockModelProvider(
        responses=[write, write if replay else final(artifacts=["result.txt"])]
    )
    settings = Settings(
        forge_dev_mode_enabled=True,
        forge_dev_context_limit=32768,
        forge_dev_context_checkpoint_ratio=0.05,
        forge_dev_context_reserve_tokens=0,
        tool_workspace_root=str(tmp_path),
    )
    async with get_session_factory()() as session:
        runtime = AgentRuntime(session, settings=settings, provider=provider)
        if replay:
            with pytest.raises(AgentRuntimeDomainError) as error:
                await runtime.execute(UUID(task["id"]))
            assert error.value.code == "DUPLICATE_TOOL_LOOP"
        else:
            run = await runtime.execute(UUID(task["id"]))
            assert run.status.value == "SUCCEEDED"
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
        events = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == UUID(task["id"]), Event.type == "DEV_CONTEXT_ROLLOVER"
                )
            )
        )
        assert len(calls) == 1
        assert len(events) == 1
        assert events[0].details["tool_steps"] == 1
        assert provider.requests[1].tool_exchanges == ()
        assert "Forge context handoff" in provider.requests[1].user_prompt


async def test_missing_third_deliverable_rollover_cannot_force_completion(
    client, monkeypatch
):
    company = await create_company(client)
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
            "title": "Three-file rollover regression",
            "input": {"deliverables": ["index.html", "styles.css", "app.js"]},
            "acceptance_criteria": [
                "index.html, styles.css, and app.js exist and link correctly"
            ],
            "max_iterations": 2,
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    provider = MockModelProvider(
        responses=[
            tool(
                "filesystem.write",
                path="index.html",
                content='<link rel="stylesheet" href="styles.css"><script src="app.js"></script>',
            ),
            tool("filesystem.write", path="styles.css", content="body{margin:0}"),
            tool("git.status"),
            tool("git.diff"),
            final("All three deliverables are complete.", ["index.html", "styles.css", "app.js"]),
        ]
    )
    threshold_checks = 0

    def trigger_one_rollover(self, request, limit, *, reserve_tokens=0):
        nonlocal threshold_checks
        threshold_checks += 1
        return threshold_checks == 4

    monkeypatch.setattr(ContextRollover, "needed", trigger_one_rollover)
    settings = Settings(
        forge_dev_mode_enabled=True,
        forge_dev_context_reserve_tokens=0,
    )
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(task["id"]), checkpoint=True
        )
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=provider).execute(
                UUID(task["id"])
            )
        assert raised.value.code == "UNVERIFIED_EXECUTION_CLAIM"
        rollover = await session.scalar(
            select(Event).where(
                Event.task_id == UUID(task["id"]), Event.type == "DEV_CONTEXT_ROLLOVER"
            )
        )
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == UUID(task["id"]))
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
    assert rollover is not None
    assert rollover.details["completion_safety"]["incomplete_deliverables"] == ["app.js"]
    assert rollover.details["continuation_mode"] == "EXECUTION_REPAIR"
    assert "app.js" in rollover.details["next_action"]
    assert "final result" not in rollover.details["next_action"]
    assert provider.requests[3].tools
    assert provider.requests[4].tools
    assert [item.tool_name for item in calls].count("filesystem.write") == 2
    assert not any(
        item.tool_name == "filesystem.write" and item.arguments.get("path") == "app.js"
        for item in calls
    )


async def test_development_budget_preflight_persists_recoverable_handoff(
    client, tmp_path, monkeypatch
):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client, company["id"], role="LEAD_ENGINEER", permissions={"filesystem.write": True}
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Preserve work before budget stop",
            "acceptance_criteria": ["result.txt exists"],
            "max_iterations": 2,
        },
    )
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")

    async def checkpoint(*args, **kwargs):
        return {"created": True, "checkpoint": "recovery", "diff_summary": "result.txt"}

    monkeypatch.setattr(
        "app.development.bootstrap.ProjectBootstrapService.checkpoint_recovery", checkpoint
    )
    provider = MockModelProvider(
        responses=[tool("filesystem.write", path="result.txt", content="preserved")],
        usage=ModelUsage(input_tokens=14_000, output_tokens=10, total_tokens=14_010),
    )
    settings = Settings(
        max_input_tokens_per_task=15_000,
        forge_dev_context_reserve_tokens=0,
        tool_workspace_root=str(tmp_path),
    )
    async with get_session_factory()() as session:
        runtime = AgentRuntime(session, settings=settings, provider=provider)
        with pytest.raises(AgentRuntimeDomainError) as error:
            await runtime.execute(UUID(task["id"]))
        assert error.value.code == "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED"
        handoff = await session.scalar(
            select(Event).where(
                Event.task_id == UUID(task["id"]), Event.type == "DEV_BUDGET_HANDOFF"
            )
        )
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
        assert provider.call_count == 1
        assert len(calls) == 1
        assert handoff is not None
        assert handoff.details["checkpoint"]["created"] is True
        assert handoff.details["files_modified"] == ["result.txt"]
        assert (tmp_path / company["id"] / project["id"] / "result.txt").read_text() == "preserved"

        await TaskStateMachine(session).transition(
            UUID(task["id"]), TaskStatus.FAILED, "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED"
        )
        await EfficientRuntimeService(session, settings).approve_input_budget_resume(
            UUID(task["id"]), 100_000
        )
        resumed = MockModelProvider(
            responses=[tool("filesystem.write", path="result.txt", content="preserved")]
        )
        with pytest.raises(AgentRuntimeDomainError) as replay_error:
            await AgentRuntime(session, settings=settings, provider=resumed).execute(
                UUID(task["id"])
            )
        assert replay_error.value.code == "DUPLICATE_TOOL_LOOP"
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
        assert len(calls) == 1


async def test_large_tool_result_is_durable_but_bounded_in_model_continuation(client, tmp_path):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client, company["id"], role="LEAD_ENGINEER", permissions={"filesystem.read": True}
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    workspace = tmp_path / company["id"] / project["id"]
    workspace.mkdir(parents=True)
    content = "large-evidence\n" * 2000
    (workspace / "large.txt").write_text(content)
    provider = MockModelProvider(
        responses=[
            tool("filesystem.read", path="large.txt"),
            final("Inspected the requested source."),
        ]
    )
    async with get_session_factory()() as session:
        await AgentRuntime(
            session,
            settings=Settings(
                tool_workspace_root=str(tmp_path), forge_dev_context_reserve_tokens=0
            ),
            provider=provider,
        ).execute(UUID(task["id"]))
        call = await session.scalar(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        assert call is not None
        assert call.result["content"] == content
        injected = provider.requests[1].tool_exchanges[0].response
        assert injected["truncated"] is True
        assert len(injected["result"]["content"]) < len(content)
        assert injected["result"]["content_sha256"]


async def test_isolated_self_development_test_debug_qa_checkpoint_memory(client, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.email", "forge@example.invalid")
    git("config", "user.name", "Forge")
    (source / "package.json").write_text(
        json.dumps({"name": "fixture", "scripts": {"test": "node test.mjs"}})
    )
    (source / "add.mjs").write_text("export const add = (a, b) => 0;\n")
    (source / "test.mjs").write_text(
        "import {add} from './add.mjs'; import assert from 'node:assert/strict'; "
        "assert.equal(add(1, 2), 3);\n"
    )
    git("add", ".")
    git("commit", "-m", "fixture")
    baseline = git("rev-parse", "HEAD")
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.write": True, "development.execute": True, "git.read": True},
    )
    await create_agent(
        client,
        company["id"],
        role="QA",
        permissions={"development.execute": True, "git.read": True},
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Repair add",
            "input": {"self_development": True},
            "acceptance_criteria": ["`add.mjs` exists"],
            "max_iterations": 2,
        },
    )
    assert response.status_code == 201
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(
        forge_dev_mode_enabled=True,
        forge_self_development_enabled=True,
        forge_dev_repository_path=str(source),
        forge_dev_allowed_repository=str(source),
        forge_dev_worktree_root="/workspaces",
        tool_workspace_root="/workspaces",
        product_qa_enabled=False,
    )
    provider = MockModelProvider(
        responses=[
            tool("development.execute", action="NODE_TEST"),
            tool(
                "filesystem.write", path="add.mjs", content="export const add = (a, b) => a + b;\n"
            ),
            tool("development.execute", action="NODE_TEST"),
            tool("git.diff"),
            tool("git.status"),
            final("Repaired add.mjs; tests passed.", ["add.mjs"]),
        ]
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(task["id"]), defer_review=True
        )
        qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
            UUID(task["id"]), run.task_run_id
        )
        assert qa.decision.value == "PASS"
        event = await session.scalar(
            select(Event).where(
                Event.task_id == UUID(task["id"]), Event.type == "DEV_WORKTREE_REVIEW_READY"
            )
        )
        assert event.details["checkpoint_commit"] != baseline
        brain = await session.scalar(
            select(ProjectKnowledgeIndex).where(
                ProjectKnowledgeIndex.project_id == UUID(project["id"])
            )
        )
        assert brain.state["pending_review"]["status"] == "VERIFIED_PENDING_HUMAN_REVIEW"
        assert git("rev-parse", "main") == baseline
        assert (source / "add.mjs").read_text().endswith("=> 0;\n")
    status = await client.get(f"/api/v1/tasks/{task['id']}/dev-mode")
    assert status.status_code == 200
    assert status.json()["human_promotion_required"]


async def test_verified_routing_rejects_fake_tools_and_accounts_probes(
    client, tmp_path, monkeypatch
):
    from dataclasses import replace

    from pydantic import SecretStr

    from app.agent_runtime.contracts import ModelResponse, ModelToolCall
    from app.agent_runtime.dev_models import prober_for
    from app.agent_runtime.probes import CapabilityProber
    from app.domain.models import ModelCallRecord
    from tests.test_dev_probes import FINAL

    prober_for.cache_clear()

    async def profiles(profiles, **kwargs):
        return tuple(
            replace(p, supported_parameters=frozenset({"tools", "response_format"}))
            for p in profiles
        )

    monkeypatch.setattr("app.agent_runtime.runtime.refresh_profiles", profiles)
    prober = CapabilityProber()
    monkeypatch.setattr("app.agent_runtime.runtime.prober_for", lambda *args: prober)

    class BehavioralProvider:
        name, paid = "openrouter", False

        async def generate(self, request):
            if request.system_prompt == "Follow the probe protocol exactly.":
                if not request.tools or request.tool_exchanges:
                    return ModelResponse(output=FINAL)
                if request.model == "fake:free":
                    return ModelResponse(output="I called the tool")
                return ModelResponse(
                    output={},
                    tool_call=ModelToolCall(
                        "forge_probe.echo", {"value": "FORGE_PROBE_OK"}, "echo-1"
                    ),
                )
            if not request.tool_exchanges:
                return ModelResponse(
                    output=tool("filesystem.write", path="ok.txt", content="ok"),
                    tool_call=ModelToolCall(
                        "filesystem.write", {"path": "ok.txt", "content": "ok"}, "write-1"
                    ),
                )
            return ModelResponse(output=final(artifacts=["ok.txt"]))

    monkeypatch.setattr(
        "app.agent_runtime.runtime.provider_for_profile", lambda *args: BehavioralProvider()
    )
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client, company["id"], role="LEAD_ENGINEER", permissions={"filesystem.write": True}
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(
        forge_dev_mode_enabled=True,
        openrouter_enabled=True,
        openrouter_api_key=SecretStr("test-only"),
        tool_workspace_root=str(tmp_path),
        model_catalog=[
            {
                "alias": alias,
                "model": model,
                "provider": "openrouter",
                "tier": "FREE",
                "paid": False,
                "capabilities": [
                    "TEXT",
                    "CODING",
                    "REASONING",
                    "STRUCTURED_OUTPUT",
                    "TOOL_CALLING",
                ],
            }
            for alias, model in [("a", "fake:free"), ("b", "healthy:free")]
        ]
        + [
            {
                "alias": "healthy-metadata-duplicate",
                "model": "healthy:free",
                "provider": "openrouter",
                "tier": "FREE",
                "paid": False,
                "capabilities": ["TEXT"],
            }
        ],
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings).execute(UUID(task["id"]))
        assert run.model_id == "healthy:free"
        events = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == UUID(task["id"]), Event.type == "DEV_MODEL_ROUTING"
                )
            )
        )
        assert any(p["status"] == "protocol_failure" for e in events for p in e.details["probes"])
        assert not any(
            rejected.get("model") == "healthy:free"
            and rejected.get("reason") == "DECLARED_CAPABILITY_MISSING"
            for event in events
            for rejected in event.details["rejected"]
        )
        calls = list(
            await session.scalars(
                select(ModelCallRecord).where(ModelCallRecord.task_id == UUID(task["id"]))
            )
        )
        assert len([c for c in calls if c.agent_role == "CAPABILITY_PROBE"]) == 6


async def test_malformed_tool_call_is_repaired_by_declared_dedicated_tool(client):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"development.execute": True, "git.read": True},
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings()
    provider = MockModelProvider(
        responses=[
            tool("development.execute", action="GIT_STATUS"),
            tool("git.status"),
            final("Corrected the malformed request using the dedicated Git tool."),
        ]
    )
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(task["id"]), checkpoint=True
        )
        await AgentRuntime(
            session,
            settings=settings,
            provider=provider,
        ).execute(UUID(task["id"]))
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == UUID(task["id"]))
                .order_by(ToolCall.created_at)
            )
        )
        events = list(await session.scalars(select(Event).where(Event.task_id == UUID(task["id"]))))
        assert calls[0].error["details"]["suggested_tool"] == "git.status"
        assert calls[0].error["details"]["repair_attempt"] == 1
        assert calls[1].tool_name == "git.status"
        assert calls[1].status.value == "SUCCEEDED"
        assert any(item.type == "DEV_TOOL_ARGUMENT_REPAIR_SUCCEEDED" for item in events)


async def test_repeated_malformed_tool_call_hits_repair_bound(client, tmp_path):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"development.execute": True},
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    malformed = tool("development.execute", action="GIT_STATUS")
    provider = MockModelProvider(responses=[malformed, malformed])
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(
                session,
                settings=Settings(tool_workspace_root=str(tmp_path)),
                provider=provider,
            ).execute(UUID(task["id"]))
        assert raised.value.code == "TOOL_ARGUMENT_REPAIR_LIMIT_EXHAUSTED"
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == UUID(task["id"]))
                .order_by(ToolCall.created_at)
            )
        )
        assert len(calls) == 2
        assert calls[-1].error["details"]["repair_attempt"] == 2


async def test_observation_reuse_is_invalidated_by_write(client, tmp_path):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.read": True, "filesystem.write": True},
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    workspace = tmp_path / company["id"] / project["id"]
    workspace.mkdir(parents=True)
    (workspace / "note.txt").write_text("old")
    provider = MockModelProvider(
        responses=[
            tool("filesystem.read", path="note.txt"),
            tool("filesystem.read", path="note.txt"),
            tool("filesystem.write", path="note.txt", content="new"),
            tool("filesystem.read", path="note.txt"),
            final("Updated and re-read note.txt.", ["note.txt"]),
        ]
    )
    async with get_session_factory()() as session:
        await AgentRuntime(
            session,
            settings=Settings(tool_workspace_root=str(tmp_path)),
            provider=provider,
        ).execute(UUID(task["id"]))
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
        metric = await session.scalar(
            select(TaskRuntimeMetric).where(TaskRuntimeMetric.task_id == UUID(task["id"]))
        )
        assert [item.tool_name for item in calls].count("filesystem.read") == 2
        assert metric.reused_observations == 1
        assert metric.stale_observation_invalidations >= 1
        assert (workspace / "note.txt").read_text() == "new"


async def test_alternating_unchanged_reads_stop_as_development_stagnation(client, tmp_path):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.list": True, "filesystem.read": True},
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    workspace = tmp_path / company["id"] / project["id"]
    workspace.mkdir(parents=True)
    (workspace / "note.txt").write_text("unchanged")
    provider = MockModelProvider(
        responses=[
            tool("filesystem.list", path="."),
            tool("filesystem.read", path="note.txt"),
            tool("filesystem.list", path="."),
            tool("filesystem.read", path="note.txt"),
            tool("filesystem.list", path="."),
        ]
    )
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(
                session,
                settings=Settings(tool_workspace_root=str(tmp_path), forge_dev_stagnation_limit=3),
                provider=provider,
            ).execute(UUID(task["id"]))
        assert raised.value.code == "DEVELOPMENT_STAGNATION"
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
        assert len(calls) == 2


async def test_novanote_like_static_task_rolls_repairs_and_reaches_review_boundary(client):
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
        },
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    body = "x" * 6000
    provider = MockModelProvider(
        responses=[
            tool("filesystem.write", path="index.html", content=f"<main>{body}</main>"),
            tool("filesystem.write", path="styles.css", content=body),
            tool("filesystem.write", path="app.js", content=body),
            tool("development.execute", action="GIT_STATUS"),
            tool("git.status"),
            tool("git.diff"),
            final("Created and verified the three-file static landing page.", ["index.html"]),
        ]
    )
    settings = Settings(
        forge_dev_context_checkpoint_ratio=0.3,
        forge_dev_context_reserve_tokens=0,
    )
    async with get_session_factory()() as session:
        await ProjectBootstrapService(session, settings=settings).ensure_task(
            UUID(task["id"]), checkpoint=True
        )
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(task["id"]), defer_review=True
        )
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == UUID(task["id"]))
                .order_by(ToolCall.created_at)
            )
        )
        rollovers = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == UUID(task["id"]),
                    Event.type == "DEV_CONTEXT_ROLLOVER",
                )
            )
        )
        assert run.status.value == "SUCCEEDED"
        assert [item.tool_name for item in calls].count("filesystem.write") == 3
        assert len(rollovers) <= 2
        assert provider.call_count == 7
        assert provider.requests[-1].tools == ()
        estimated_fixture_input = sum(
            ContextRollover.estimated_input_tokens(request, reserve_tokens=0)
            for request in provider.requests
        )
        assert estimated_fixture_input <= 60_000
        if rollovers:
            assert "reread or rewrite unchanged files" in rollovers[-1].details["next_action"]
