from datetime import UTC, datetime
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select

from app.agent_runtime.builders import ContextRecord, InstructionBuilder
from app.agent_runtime.efficiency import EfficientRuntimeService
from app.core.config import Settings
from app.development.product_qa import ProductQAService
from app.domain.enums import TaskStatus
from app.domain.models import Event, Task, TaskRuntimeBudget
from app.infrastructure.database import get_session_factory
from app.planning.mission_planner import MissionPlanner
from app.services.event_factory import EventFactory
from app.tool_system.contracts import ToolObservation, ToolRequestTurn
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


def test_clipmind_regression_fixture_detects_product_failures() -> None:
    issues = ProductQAService.inspect_sources(
        {
            "sidepanel.html": (
                '<main><input id="topic"><div class="toolbar">ClipMind</div></main>'
            ),
            "src/sidepanel.css": (
                "body { overflow-x: auto; } .toolbar { width: 760px; font-size: 10px; }"
            ),
        }
    )
    categories = {item["category"] for item in issues}
    assert "HORIZONTAL_OVERFLOW" in categories
    assert "VISUAL_HIERARCHY" in categories
    assert "MISSING_FORM_LABEL" in categories
    assert all(item["evidence"] for item in issues)


def test_responsive_accessible_fixture_has_no_major_issue() -> None:
    issues = ProductQAService.inspect_sources(
        {
            "sidepanel.html": (
                '<main><h1>ClipMind</h1><label for="topic">Topic</label><input id="topic"></main>'
            ),
            "src/sidepanel.css": (
                "main { width: 100%; max-width: 100%; } input { width: 100%; max-width: 100%; } "
                "@media (max-width: 390px) { main { padding: 12px; } }"
            ),
        }
    )
    assert not [item for item in issues if item["severity"] in {"BLOCKING", "MAJOR"}]


def test_context_delta_omits_older_observation_content() -> None:
    context = ContextRecord(
        company={"id": str(uuid4()), "name": "Forge", "goal": "Ship"},
        project={"id": str(uuid4()), "name": "ClipMind", "goal": "Extension"},
        task={"id": str(uuid4()), "title": "Build UI", "input": {}, "acceptance_criteria": []},
        agent={"id": str(uuid4()), "name": "Developer", "role": "DEVELOPER"},
        project_knowledge={"architecture_summary": "MV3 extension"},
    )
    observations = [
        ToolObservation(
            tool_call_id=uuid4(),
            tool="filesystem.read",
            status="success",
            result={"path": f"src/{index}.ts", "content": f"UNIQUE_CONTENT_{index}"},
        )
        for index in range(5)
    ]
    built = InstructionBuilder().build(
        context,
        observations=observations,
        turn_number=6,
        recent_observation_limit=2,
        duplicate_warning=True,
    )
    assert "Continue from this task-scoped context delta" in built.user_prompt
    assert "UNIQUE_CONTENT_0" not in built.user_prompt
    assert "UNIQUE_CONTENT_3" in built.user_prompt
    assert "repeated unchanged action" in built.system_prompt


def test_planner_requires_measurable_frontend_product_criteria() -> None:
    prompt = MissionPlanner.SYSTEM_PROMPT
    assert "390x844" in prompt
    assert "horizontal overflow" in prompt
    assert "keyboard focus" in prompt
    assert "do not claim screenshots" in prompt.lower()


async def test_runtime_efficiency_api_uses_durable_agent_run_usage(
    client: AsyncClient,
) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role="RESEARCHER")
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    execution = await client.post(f"/api/v1/tasks/{task['id']}/execute", json={})
    assert execution.status_code == 200

    response = await client.get(f"/api/v1/tasks/{task['id']}/runtime-efficiency")
    assert response.status_code == 200
    payload = response.json()
    assert payload["model_calls"] == 1
    assert payload["input_tokens"] == 12
    assert payload["output_tokens"] == 18
    assert payload["cached_tokens"] == 2
    assert payload["limits"]["model_calls"] == 24
    assert float(payload["estimated_cost"]) == 0


async def test_duplicate_guard_only_counts_consecutive_unchanged_tool_requests(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"duplicate-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role="DEVELOPER")
    task = await create_task(client, company["id"], project["id"], agent["id"])
    read = ToolRequestTurn(
        type="tool_call", tool_name="filesystem.read", arguments={"path": "index.html"}
    )
    write = ToolRequestTurn(
        type="tool_call",
        tool_name="filesystem.write",
        arguments={"path": "index.html", "content": "changed"},
    )
    async with get_session_factory()() as session:
        service = EfficientRuntimeService(session, Settings())
        task_id = UUID(task["id"])
        assert await service.record_tool_request(task_id, read) == 0
        assert await service.record_tool_request(task_id, read) == 1
        assert await service.record_tool_request(task_id, write) == 0
        # Reading the same path after a write is verification, not a duplicate loop.
        assert await service.record_tool_request(task_id, read) == 0
        assert await service.record_tool_request(task_id, read) == 1
        assert await service.record_tool_request(task_id, read) == 2
        await session.rollback()


async def test_human_budget_resume_preserves_usage_and_adds_only_approved_tokens(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"budget-resume-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role="DEVELOPER")
    task = await create_task(client, company["id"], project["id"], agent["id"])
    task_id = UUID(task["id"])
    async with get_session_factory()() as session:
        model = await session.get(Task, task_id)
        assert model is not None
        budget = await EfficientRuntimeService(session, Settings()).enforce_budget(model)
        model.status = TaskStatus.FAILED
        model.iteration = 1
        model.max_iterations = 1
        model.completed_at = datetime.now(UTC)
        model.terminal_reason = {
            "code": "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED",
            "message": "Task input-token budget exhausted.",
        }
        budget.consumed_input_tokens = 240_000
        budget.stopped_reason = "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED"
        await session.commit()

    response = await client.post(
        f"/api/v1/tasks/{task_id}/resume-input-budget",
        json={"additional_input_tokens": 25_000},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "QUEUED"
    assert response.json()["max_iterations"] == 2
    async with get_session_factory()() as session:
        budget = await session.scalar(
            select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == task_id)
        )
        event = await session.scalar(
            select(Event).where(
                Event.task_id == task_id, Event.type == "DEV_BUDGET_RESUME_APPROVED"
            )
        )
        handoff = await session.scalar(
            select(Event).where(Event.task_id == task_id, Event.type == "DEV_BUDGET_HANDOFF")
        )
        assert budget is not None
        assert budget.max_input_tokens == 265_000
        assert budget.consumed_input_tokens == 240_000
        assert budget.stopped_reason is None
        assert event is not None
        assert handoff is not None
        assert handoff.details["legacy_reconstructed"] is True


async def test_human_context_resume_is_single_use_and_preserves_bounds(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"context-resume-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role="DEVELOPER")
    task = await create_task(client, company["id"], project["id"], agent["id"])
    task_id = UUID(task["id"])
    async with get_session_factory()() as session:
        model = await session.get(Task, task_id)
        assert model is not None
        budget = await EfficientRuntimeService(session, Settings()).enforce_budget(model)
        model.status = TaskStatus.FAILED
        model.iteration = 1
        model.max_iterations = 1
        model.completed_at = datetime.now(UTC)
        model.terminal_reason = {
            "code": "MODEL_CONTEXT_ROLLOVER_LIMIT_EXHAUSTED",
            "message": "Context rollover bound reached.",
        }
        budget.consumed_input_tokens = 219_681
        await EventFactory(session).create(
            company_id=model.company_id,
            project_id=model.project_id,
            agent_id=model.assigned_agent_id,
            task_id=model.id,
            correlation_id=model.id,
            event_type="DEV_CONTEXT_FAILURE_HANDOFF",
            message="Context recovery handoff.",
            payload={
                "stop_reason": "MODEL_CONTEXT_ROLLOVER_LIMIT_EXHAUSTED",
                "checkpoint": {"created": True, "commit": "deadbeef"},
                "rollover_count": 4,
                "rollover_limit": 4,
                "input_tokens_consumed": 219_681,
                "mutation_fingerprints": ["write:index.html"],
            },
        )
        await session.commit()

    response = await client.post(f"/api/v1/tasks/{task_id}/resume-context")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "QUEUED"
    assert response.json()["max_iterations"] == 2

    duplicate = await client.post(f"/api/v1/tasks/{task_id}/resume-context")
    assert duplicate.status_code == 409
    async with get_session_factory()() as session:
        budget = await session.scalar(
            select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == task_id)
        )
        approval = await session.scalar(
            select(Event).where(
                Event.task_id == task_id, Event.type == "DEV_CONTEXT_RESUME_APPROVED"
            )
        )
        assert budget is not None
        assert budget.max_input_tokens == 240_000
        assert budget.consumed_input_tokens == 219_681
        assert approval is not None
        assert approval.details["rollover_limit_unchanged"] is True
