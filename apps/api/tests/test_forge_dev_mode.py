from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import ToolCallStatus
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import ToolCall
from app.infrastructure.database import get_session_factory
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def test_lead_engineer_write_is_completed_only_with_execution_evidence(
    client: AsyncClient,
) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={"filesystem.write": True},
    )
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        title="Create hello.txt containing FORGE_SELF_DEV_OK",
    )
    await transition_task(client, task["id"], "QUEUED")
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "hello.txt", "content": "FORGE_SELF_DEV_OK"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Forge wrote hello.txt.",
                    "output": {
                        "artifacts": ["hello.txt"],
                        "details": ["The successful ToolCall is authoritative."],
                        "execution_claims": [
                            {"kind": "FILE_MUTATION", "reference": "hello.txt"}
                        ],
                    },
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(model_provider="mock", tool_workspace_root="/tmp/forge-dev-mode-tests")

    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(task["id"])
        )
        calls = list(
            await session.scalars(select(ToolCall).where(ToolCall.task_id == UUID(task["id"])))
        )

    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(company["id"]), UUID(project["id"])
    )
    assert run.response is not None
    assert run.response["status"] == "completed"
    assert (Path(workspace) / "hello.txt").read_text() == "FORGE_SELF_DEV_OK"
    assert len(calls) == 1
    assert calls[0].status == ToolCallStatus.SUCCEEDED
    assert calls[0].result["path"] == "hello.txt"


async def test_lead_engineer_text_only_write_claim_is_not_completion(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug="hallucinated-write")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role="LEAD_ENGINEER")
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        title="Create hello.txt containing FORGE_SELF_DEV_OK",
    )
    await transition_task(client, task["id"], "QUEUED")
    provider = MockModelProvider(
        response={
            "type": "final",
            "result": {
                "status": "completed",
                "summary": "Successfully created hello.txt.",
                "output": {"artifacts": [], "details": [], "execution_claims": []},
                "notes": [],
            },
        }
    )
    settings = Settings(model_provider="mock", tool_workspace_root="/tmp/forge-dev-mode-tests")

    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=provider).execute(
                UUID(task["id"])
            )
        assert raised.value.code == "UNVERIFIED_EXECUTION_CLAIM"

    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(company["id"]), UUID(project["id"])
    )
    assert not (workspace / "hello.txt").exists()
