import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.domain.enums import TaskRunStatus, TaskStatus
from app.domain.models import Event, Task, TaskRun
from app.infrastructure.database import get_session_factory
from app.services.task_state_machine import TaskStateMachine
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def create_assigned_task(
    client: AsyncClient, *, max_iterations: int = 5
) -> dict[str, object]:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task = await create_task(
        client,
        company["id"],
        project["id"],
        agent["id"],
        max_iterations=max_iterations,
    )
    return {"company": company, "project": project, "agent": agent, "task": task}


def test_transition_table_contains_exact_phase_03_rules() -> None:
    expected = {
        TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
        TaskStatus.QUEUED: {
            TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        },
        TaskStatus.IN_PROGRESS: {
            TaskStatus.REVIEW,
            TaskStatus.FIX_REQUIRED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
        TaskStatus.REVIEW: {
            TaskStatus.DONE,
            TaskStatus.FIX_REQUIRED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
        TaskStatus.FIX_REQUIRED: {
            TaskStatus.QUEUED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
        TaskStatus.DONE: set(),
        TaskStatus.FAILED: set(),
        TaskStatus.CANCELLED: set(),
    }

    assert {
        state: set(targets) for state, targets in TaskStateMachine.ALLOWED_TRANSITIONS.items()
    } == expected


async def test_retry_lifecycle_updates_timestamps_iterations_runs_and_events(
    client: AsyncClient,
) -> None:
    domain = await create_assigned_task(client, max_iterations=2)
    task = domain["task"]
    assert isinstance(task, dict)
    task_id = task["id"]

    assert task["iteration"] == 0
    assert task["started_at"] is None
    assert task["completed_at"] is None

    await transition_task(client, task_id, "QUEUED", "Ready for execution")
    first_start = await transition_task(
        client, task_id, "IN_PROGRESS", "Developer worker picked up the task"
    )
    original_started_at = first_start["started_at"]
    assert first_start["iteration"] == 1
    assert original_started_at is not None

    review = await transition_task(client, task_id, "REVIEW", "Implementation completed")
    assert review["completed_at"] is None
    first_runs = (await client.get(f"/api/v1/tasks/{task_id}/runs")).json()
    assert len(first_runs) == 1
    assert first_runs[0]["iteration"] == 1
    assert first_runs[0]["status"] == "SUCCEEDED"
    assert first_runs[0]["completed_at"] is not None

    await transition_task(client, task_id, "FIX_REQUIRED", "Mobile navigation is broken")
    await transition_task(client, task_id, "QUEUED", "Fix is ready to retry")
    second_start = await transition_task(client, task_id, "IN_PROGRESS", "Retry started")
    assert second_start["iteration"] == 2
    assert second_start["started_at"] == original_started_at

    await transition_task(client, task_id, "REVIEW", "Fix completed")
    done = await transition_task(client, task_id, "DONE", "QA accepted the result")
    assert done["completed_at"] is not None

    runs = (await client.get(f"/api/v1/tasks/{task_id}/runs")).json()
    assert [(run["iteration"], run["status"]) for run in runs] == [
        (1, "SUCCEEDED"),
        (2, "SUCCEEDED"),
    ]

    async with get_session_factory()() as session:
        transition_events = list(
            await session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.type == "TASK_STATUS_CHANGED")
                .order_by(Event.created_at)
            )
        )

    start_event = next(
        event
        for event in transition_events
        if event.details["from_status"] == "QUEUED" and event.details["to_status"] == "IN_PROGRESS"
    )
    assert start_event.company_id is not None
    assert start_event.project_id is not None
    assert start_event.agent_id is not None
    assert start_event.details == {
        "from_status": "QUEUED",
        "to_status": "IN_PROGRESS",
        "reason": "Developer worker picked up the task",
        "iteration": 1,
    }


@pytest.mark.parametrize(
    ("path", "invalid_target"),
    [
        ([], "DONE"),
        (["QUEUED", "IN_PROGRESS", "REVIEW", "DONE"], "QUEUED"),
        (["QUEUED", "FAILED"], "IN_PROGRESS"),
        (["CANCELLED"], "CREATED"),
        (["QUEUED", "IN_PROGRESS", "REVIEW", "FIX_REQUIRED"], "DONE"),
    ],
)
async def test_invalid_and_terminal_transitions_return_conflict(
    client: AsyncClient, path: list[str], invalid_target: str
) -> None:
    domain = await create_assigned_task(client)
    task = domain["task"]
    assert isinstance(task, dict)
    for target in path:
        await transition_task(client, task["id"], target)

    current = path[-1] if path else "CREATED"
    response = await client.post(
        f"/api/v1/tasks/{task['id']}/transition",
        json={"target_status": invalid_target},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "INVALID_TASK_TRANSITION",
            "message": f"Transition from {current} to {invalid_target} is not allowed",
        }
    }


async def test_execution_requires_assignment(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    task = await create_task(client, company["id"], project["id"], None)
    await transition_task(client, task["id"], "QUEUED")

    response = await client.post(
        f"/api/v1/tasks/{task['id']}/transition",
        json={"target_status": "IN_PROGRESS"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "TASK_UNASSIGNED",
        "message": "Task must be assigned before execution",
    }


async def test_max_iterations_fails_without_creating_another_run(
    client: AsyncClient,
) -> None:
    domain = await create_assigned_task(client, max_iterations=1)
    task = domain["task"]
    assert isinstance(task, dict)
    task_id = task["id"]

    for target in ["QUEUED", "IN_PROGRESS", "REVIEW", "FIX_REQUIRED", "QUEUED"]:
        await transition_task(client, task_id, target)

    exceeded = await transition_task(
        client, task_id, "IN_PROGRESS", "Attempt beyond configured limit"
    )

    assert exceeded["status"] == "FAILED"
    assert exceeded["iteration"] == 1
    assert exceeded["completed_at"] is not None
    runs = (await client.get(f"/api/v1/tasks/{task_id}/runs")).json()
    assert len(runs) == 1
    assert runs[0]["status"] == "SUCCEEDED"

    async with get_session_factory()() as session:
        max_event = await session.scalar(
            select(Event).where(
                Event.task_id == task_id,
                Event.type == "TASK_MAX_ITERATIONS_EXCEEDED",
            )
        )
    assert max_event is not None
    assert max_event.details["iteration"] == 1
    assert max_event.details["max_iterations"] == 1


async def test_failed_execution_closes_active_run(client: AsyncClient) -> None:
    domain = await create_assigned_task(client)
    task = domain["task"]
    assert isinstance(task, dict)
    await transition_task(client, task["id"], "QUEUED")
    await transition_task(client, task["id"], "IN_PROGRESS")

    failed = await transition_task(client, task["id"], "FAILED", "Execution crashed")
    runs = (await client.get(f"/api/v1/tasks/{task['id']}/runs")).json()

    assert failed["status"] == "FAILED"
    assert failed["completed_at"] is not None
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error"] == {"reason": "Execution crashed"}
    assert runs[0]["completed_at"] is not None


async def test_concurrent_execution_start_creates_exactly_one_run(
    client: AsyncClient,
) -> None:
    domain = await create_assigned_task(client)
    task = domain["task"]
    assert isinstance(task, dict)
    task_id = task["id"]
    await transition_task(client, task_id, "QUEUED")

    first, second = await asyncio.gather(
        client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"target_status": "IN_PROGRESS", "reason": "Worker A"},
        ),
        client.post(
            f"/api/v1/tasks/{task_id}/transition",
            json={"target_status": "IN_PROGRESS", "reason": "Worker B"},
        ),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    losing_response = first if first.status_code == 409 else second
    assert losing_response.json()["error"]["code"] == "INVALID_TASK_TRANSITION"

    async with get_session_factory()() as session:
        persisted_task = await session.get(Task, task_id)
        run_count = await session.scalar(
            select(func.count(TaskRun.id)).where(TaskRun.task_id == task_id)
        )
        active_count = await session.scalar(
            select(func.count(TaskRun.id)).where(
                TaskRun.task_id == task_id,
                TaskRun.status == TaskRunStatus.STARTED,
            )
        )

    assert persisted_task is not None
    assert persisted_task.status == TaskStatus.IN_PROGRESS
    assert persisted_task.iteration == 1
    assert run_count == 1
    assert active_count == 1


async def test_missing_task_run_history_returns_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000/runs")

    assert response.status_code == 404
