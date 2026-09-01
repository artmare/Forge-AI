import asyncio
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent_runtime.builders import ContextBuilder, InstructionBuilder
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import TaskReviewDecision, TaskStatus
from app.domain.models import Event, Task, TaskReview
from app.infrastructure.database import get_session_factory
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def task_in_review(
    client: AsyncClient,
    *,
    slug: str = "task-review-company",
    permissions: dict[str, bool] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    company = await create_company(client, slug=slug)
    project = await create_project(client, str(company["id"]))
    agent = await create_agent(
        client,
        str(company["id"]),
        permissions=permissions,
    )
    task = await create_task(
        client,
        str(company["id"]),
        str(project["id"]),
        str(agent["id"]),
        max_iterations=3,
    )
    await transition_task(client, str(task["id"]), "QUEUED")
    run = await client.post(f"/api/v1/tasks/{task['id']}/execute", json={})
    assert run.status_code == 200, run.text
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).json()["status"] == "REVIEW"
    return task, run.json()


async def test_request_fix_validation_requires_bounded_feedback(
    client: AsyncClient,
) -> None:
    task, _run = await task_in_review(client)
    task_id = str(task["id"])

    missing = await client.post(f"/api/v1/tasks/{task_id}/request-fix", json={})
    whitespace = await client.post(
        f"/api/v1/tasks/{task_id}/request-fix", json={"feedback": "   \n "}
    )
    oversized = await client.post(
        f"/api/v1/tasks/{task_id}/request-fix",
        json={"feedback": "x" * 10_001},
    )

    assert [missing.status_code, whitespace.status_code, oversized.status_code] == [
        422,
        422,
        422,
    ]
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(TaskReview)) == 0
        stored = await session.get(Task, task_id)
        assert stored is not None and stored.status == TaskStatus.REVIEW


async def test_fix_feedback_is_persisted_associated_requeued_and_audited(
    client: AsyncClient,
) -> None:
    task, run = await task_in_review(client)
    task_id = str(task["id"])
    feedback = "Move the AI boundary into the MVP and define provider failure handling."

    response = await client.post(
        f"/api/v1/tasks/{task_id}/request-fix", json={"feedback": feedback}
    )
    history = (await client.get(f"/api/v1/tasks/{task_id}/reviews")).json()

    assert response.status_code == 200
    assert response.json()["status"] == TaskStatus.QUEUED.value
    assert len(history) == 1
    assert history[0]["decision"] == TaskReviewDecision.FIX_REQUESTED.value
    assert history[0]["feedback"] == feedback
    assert history[0]["task_run_id"] == run["task_run_id"]
    assert history[0]["agent_run_id"] == run["id"]
    async with get_session_factory()() as session:
        events = list(await session.scalars(select(Event).where(Event.task_id == task_id)))
    by_type = {event.type: event for event in events}
    assert "TASK_FIX_REQUESTED" in by_type
    assert by_type["TASK_FIX_REQUESTED"].details["decision"] == "FIX_REQUESTED"
    assert feedback not in str(by_type["TASK_FIX_REQUESTED"].details)
    transitions = [
        event.details["to_status"] for event in events if event.type == "TASK_STATUS_CHANGED"
    ]
    assert transitions[-2:] == ["FIX_REQUIRED", "QUEUED"]


async def test_approval_is_persisted_and_review_decisions_are_concurrency_safe(
    client: AsyncClient,
) -> None:
    task, run = await task_in_review(client)
    task_id = str(task["id"])

    approved, rejected = await asyncio.gather(
        client.post(f"/api/v1/tasks/{task_id}/approve"),
        client.post(
            f"/api/v1/tasks/{task_id}/request-fix",
            json={"feedback": "Change the architecture."},
        ),
    )

    assert sorted([approved.status_code, rejected.status_code]) == [200, 409]
    history = (await client.get(f"/api/v1/tasks/{task_id}/reviews")).json()
    assert len(history) == 1
    assert history[0]["task_run_id"] == run["task_run_id"]
    assert history[0]["agent_run_id"] == run["id"]
    task_status = (await client.get(f"/api/v1/tasks/{task_id}")).json()["status"]
    if history[0]["decision"] == TaskReviewDecision.APPROVED.value:
        assert history[0]["feedback"] is None
        assert task_status == TaskStatus.DONE.value
    else:
        assert task_status == TaskStatus.QUEUED.value


async def test_revision_context_reads_and_replaces_existing_artifact_without_new_powers(
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    company = await create_company(client, slug="revision-context-company")
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        permissions={"filesystem.read": True, "filesystem.write": True},
    )
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        max_iterations=3,
        title="Technical Plan",
    )
    await transition_task(client, task["id"], "QUEUED")
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    initial_provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "technical-plan.md",
                    "content": "# Technical Plan\n\nAI is postponed.",
                },
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Initial plan written.",
                    "output": {"artifacts": ["technical-plan.md"], "details": []},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        await AgentRuntime(session, settings=settings, provider=initial_provider).execute(
            task["id"]
        )

    feedback = (
        "AI-powered structured note generation must be part of the MVP architecture. "
        "Do not postpone AI to later. Add a minimal backend AI boundary, keep provider "
        "credentials server-side, define a structured AI response contract, privacy/data "
        "flow, provider error handling, and AI-specific tests. Explicitly choose the side "
        "panel or popup as the main editing surface."
    )
    fixed = await client.post(
        f"/api/v1/tasks/{task['id']}/request-fix", json={"feedback": feedback}
    )
    assert fixed.json()["status"] == TaskStatus.QUEUED.value

    async with get_session_factory()() as session:
        context = await ContextBuilder(session).build(task["id"])
    instructions = InstructionBuilder().build(context)
    assert context.review is not None and context.review["feedback"] == feedback
    assert context.task["execution_iteration"] == 2
    assert context.artifacts == ["technical-plan.md"]
    assert "HUMAN REVIEW FEEDBACK" in instructions.user_prompt
    assert feedback in instructions.user_prompt
    assert "revising the previous result, not starting" in instructions.user_prompt

    revised_provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.read",
                "arguments": {"path": "technical-plan.md"},
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "technical-plan.md",
                    "content": "# Technical Plan\n\nAI is part of the MVP backend boundary.",
                },
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Revised the existing plan.",
                    "output": {"artifacts": ["technical-plan.md"], "details": []},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        final_run = await AgentRuntime(
            session, settings=settings, provider=revised_provider
        ).execute(task["id"])

    first_request = revised_provider.requests[0]
    assert first_request.metadata["is_revision"] == "true"
    assert first_request.metadata["review_iteration"] == "1"
    assert feedback in first_request.user_prompt
    assert "filesystem.read" in first_request.system_prompt
    assert "filesystem.write" in first_request.system_prompt
    assert '"name":"shell.run"' not in first_request.system_prompt
    assert '"name":"browser' not in first_request.system_prompt
    assert '"name":"network' not in first_request.system_prompt
    assert "AI is postponed." in revised_provider.requests[1].user_prompt
    workspace_file = tmp_path / "workspaces" / company["id"] / project["id"] / "technical-plan.md"
    assert workspace_file.read_text(encoding="utf-8").endswith(
        "AI is part of the MVP backend boundary."
    )

    approved = await client.post(f"/api/v1/tasks/{task['id']}/approve")
    assert approved.status_code == 200
    history = (await client.get(f"/api/v1/tasks/{task['id']}/reviews")).json()
    assert [review["iteration"] for review in history] == [1, 2]
    assert [review["decision"] for review in history] == ["FIX_REQUESTED", "APPROVED"]
    assert history[1]["agent_run_id"] == str(final_run.id)
    graph = (await client.get(f"/api/v1/projects/{project['id']}/graph")).json()
    assert graph["nodes"][0]["reviews"] == history
