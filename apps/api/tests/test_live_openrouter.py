"""Opt-in external harness; only disposable test data and workspaces are used."""

import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app.agent_runtime.openrouter_catalog import OpenRouterCatalogClient
from app.agent_runtime.probes import CapabilityProber, ProbeCapability
from app.agent_runtime.providers import provider_for_profile
from app.agent_runtime.registry import ModelRegistry
from app.agent_runtime.routing import ModelCapability
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import ToolCallStatus
from app.domain.models import ToolCall
from app.infrastructure.database import get_session_factory
from tests.helpers import create_agent, create_company, create_project, create_task, transition_task

pytestmark = pytest.mark.skipif(
    os.getenv("FORGE_RUN_LIVE_OPENROUTER_TESTS", "").lower() != "true",
    reason="Live OpenRouter tests are opt-in",
)


async def test_live_probe_and_isolated_forge_write(client, tmp_path: Path):
    settings = Settings(
        openrouter_enabled=True,
        openrouter_free_only=True,
        forge_dev_mode_enabled=True,
        forge_dev_free_only=True,
        tool_workspace_root=str(tmp_path / "isolated"),
        model_request_timeout_seconds=45,
        developer_max_steps=6,
        model_transient_max_attempts=1,
        model_fallback_max_candidates=3,
    )
    if not settings.openrouter_api_key:
        pytest.skip("OpenRouter credential not configured")
    catalog = OpenRouterCatalogClient(base_url=settings.openrouter_base_url, api_key=None)
    entries = await catalog.refresh()
    await catalog.client.aclose()
    candidates = [
        entry
        for entry in entries
        if entry.free
        and {ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT}.issubset(
            entry.capabilities
        )
        and entry.model_id != "openrouter/free"
    ]
    candidates.sort(
        key=lambda e: (
            0 if e.model_id.endswith(":free") else 1,
            -len(e.supported_parameters),
            -(e.context_length or 0),
            e.model_id,
        )
    )
    prober = CapabilityProber(timeout_seconds=45)
    selected = None
    reports = []
    for entry in candidates[: settings.openrouter_live_probe_candidates]:
        provider = provider_for_profile(settings, entry.profile())
        successes = []
        for capability in ProbeCapability:
            result = await prober.probe(
                provider,
                entry.model_id,
                capability,
                metadata={
                    "provider_supported_parameters": ",".join(sorted(entry.supported_parameters))
                },
            )
            reports.append(
                {
                    "model": entry.model_id,
                    "capability": capability.value,
                    "status": result.status,
                    "failure": result.failure_category,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                }
            )
            successes.append(result.status == "verified")
            if not successes[-1]:
                break
        await provider.client.close()
        if len(successes) == 3 and all(successes):
            selected = entry
            break
    print("FORGE_LIVE_PROBES=" + json.dumps(reports))
    if selected is None:
        pytest.skip("External free-model pool unavailable or failed bounded capability probes")
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.write": True, "filesystem.read": True},
    )
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        title="Use filesystem.write to create forge_probe.txt with exact "
        "content FORGE_SELF_DEV_OK. Read it back. Then finish. "
        "No Git, shell, or other files are needed.",
    )
    await transition_task(client, task["id"], "QUEUED")
    provider = provider_for_profile(settings, selected.profile())
    registry = ModelRegistry({"default": selected.model_id})
    async with get_session_factory()() as session:
        run = await AgentRuntime(
            session, settings=settings, provider=provider, registry=registry
        ).execute(UUID(task["id"]))
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )
    await provider.client.close()
    path = Path(settings.tool_workspace_root) / company["id"] / project["id"] / "forge_probe.txt"
    assert path.read_text() == "FORGE_SELF_DEV_OK"
    writes = [c for c in calls if c.tool_name == "filesystem.write"]
    assert len(writes) == 1 and writes[0].status == ToolCallStatus.SUCCEEDED
    assert writes[0].result["path"] == "forge_probe.txt"
    assert run.status.value == "SUCCEEDED"
    print(
        "FORGE_LIVE_WRITE="
        + json.dumps(
            {
                "model": selected.model_id,
                "tool_call_id": str(writes[0].id),
                "agent_run_id": str(run.id),
                "content_verified": True,
                "execution_truth": "accepted",
            }
        )
    )
    if os.getenv("FORGE_LIVE_OPENROUTER_CODE_CHANGE", "").lower() == "true":
        await _live_code_change(client, tmp_path, settings, selected, company)


async def _live_code_change(client, tmp_path, settings, selected, company):
    """Stage 4 runs only after Stage 3 succeeds; never targets the Forge repository."""
    import subprocess

    from app.development.qa import DevelopmentWorkflowService
    from app.domain.models import Event

    repository = tmp_path / "sample-repository"
    repository.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Forge live fixture")
    git("config", "user.email", "forge@example.invalid")
    (repository / "package.json").write_text(
        json.dumps({"name": "fixture", "scripts": {"test": "node test.mjs"}})
    )
    (repository / "add.mjs").write_text("export const add = (a, b) => 0;\n")
    (repository / "test.mjs").write_text(
        "import {add} from './add.mjs'; "
        "import assert from 'node:assert/strict'; assert.equal(add(1, 2), 3);\n"
    )
    git("add", ".")
    git("commit", "-m", "sample fixture")
    baseline = git("rev-parse", "HEAD")
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={
            "filesystem.read": True,
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
        },
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
            "input": {"self_development": True},
            "max_iterations": 2,
            "title": "Fix add.mjs so add(a,b) returns a+b. Inspect files, edit only add.mjs, "
            "run development.execute NODE_TEST, inspect git.diff and git.status, then finish.",
            "acceptance_criteria": ["`add.mjs` exists"],
        },
    )
    assert response.status_code == 201
    task = response.json()
    await transition_task(client, task["id"], "QUEUED")
    settings = settings.model_copy(
        update={
            "forge_self_development_enabled": True,
            "forge_dev_repository_path": str(repository),
            "forge_dev_allowed_repository": str(repository),
            "forge_dev_worktree_root": "/workspaces",
            "tool_workspace_root": "/workspaces",
            "developer_max_steps": 12,
            "product_qa_enabled": False,
        }
    )
    provider = provider_for_profile(settings, selected.profile())
    async with get_session_factory()() as session:
        run = await AgentRuntime(
            session,
            settings=settings,
            provider=provider,
            registry=ModelRegistry({"default": selected.model_id}),
        ).execute(UUID(task["id"]), defer_review=True)
        qa = await DevelopmentWorkflowService(session, settings=settings).finalize(
            UUID(task["id"]), run.task_run_id
        )
        assert qa.decision.value == "PASS"
        checkpoint = await session.scalar(
            select(Event).where(
                Event.task_id == UUID(task["id"]), Event.type == "DEV_WORKTREE_REVIEW_READY"
            )
        )
        assert checkpoint is not None
        assert git("rev-parse", "main") == baseline
        print("FORGE_LIVE_CODE_CHANGE=" + json.dumps(checkpoint.details))
    await provider.client.close()
