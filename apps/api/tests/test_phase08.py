import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent_runtime.contracts import ModelRequest, ModelResponse, ProviderCallError
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import (
    ExecutionJobStatus,
    MissionStatus,
    PlanningRunStatus,
    ProjectStatus,
    TaskStatus,
)
from app.domain.exceptions import (
    MissionConflictError,
    MissionPlanningError,
    PaidModelCallDisabledError,
)
from app.domain.models import (
    Agent,
    Company,
    ExecutionJob,
    Mission,
    PlanningRun,
    Project,
    Task,
    TaskDependency,
)
from app.infrastructure.database import get_session_factory
from app.orchestration.orchestrator import OrchestratorService
from app.orchestration.worker_service import WorkerExecutionService
from app.planning.company_factory import CompanyFactory
from app.planning.contracts import PlanProposal
from app.planning.dependency_resolver import DependencyResolver
from app.planning.mission_planner import MissionPlanner
from app.planning.validator import PlanValidator
from app.services.task_state_machine import TaskStateMachine


def valid_proposal() -> dict:
    return {
        "project": {"name": "Brief Project", "description": "Create and verify a brief."},
        "agents": [
            {
                "key": "generalist",
                "name": "Generalist",
                "role": "GENERAL",
                "description": "Handles the bounded filesystem tasks.",
                "model_alias": "default",
                "requested_permissions": {
                    "filesystem.read": True,
                    "filesystem.write": True,
                },
            }
        ],
        "tasks": [
            {
                "key": "write",
                "title": "Write brief",
                "description": "Write brief.txt.",
                "assigned_agent_key": "generalist",
                "priority": "NORMAL",
                "input": {
                    "mock_scenario": "filesystem_write",
                    "path": "brief.txt",
                    "content": "Phase 08 brief",
                },
                "acceptance_criteria": ["brief.txt is written"],
                "max_iterations": 2,
            },
            {
                "key": "verify",
                "title": "Verify brief",
                "description": "Read brief.txt.",
                "assigned_agent_key": "generalist",
                "priority": "NORMAL",
                "input": {"mock_scenario": "filesystem_read", "path": "brief.txt"},
                "acceptance_criteria": ["brief.txt is verified"],
                "max_iterations": 2,
            },
        ],
        "dependencies": [{"task": "verify", "depends_on": "write"}],
    }


async def create_mission(client: AsyncClient, title: str = "Phase 08 Mission") -> dict:
    response = await client.post(
        "/api/v1/missions",
        json={
            "title": title,
            "goal": "Create a project brief and verify it.",
            "constraints": {"format": "text"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def plan_with(client: AsyncClient, proposal: dict, title: str = "Custom Plan") -> dict:
    mission = await create_mission(client, title)
    async with get_session_factory()() as session:
        run = await MissionPlanner(session, provider=MockModelProvider(response=proposal)).plan(
            UUID(mission["id"])
        )
    return {"mission": mission, "run": run}


async def count(session, model) -> int:  # type: ignore[no-untyped-def]
    return int(await session.scalar(select(func.count(model.id))) or 0)


async def test_mock_plan_is_valid_and_requires_human_activation(client: AsyncClient) -> None:
    mission = await create_mission(client)
    response = await client.post(f"/api/v1/missions/{mission['id']}/plan")
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == PlanningRunStatus.SUCCEEDED.value
    assert run["provider"] == "mock"
    assert Decimal(run["estimated_cost"]) == 0

    detail = (await client.get(f"/api/v1/missions/{mission['id']}")).json()
    assert detail["status"] == MissionStatus.PLAN_READY.value
    async with get_session_factory()() as session:
        assert await count(session, Company) == 0
        assert await count(session, Project) == 0
        assert await count(session, Agent) == 0
        assert await count(session, Task) == 0

    plan = (await client.get(f"/api/v1/missions/{mission['id']}/plan")).json()
    assert plan["validation"]["valid"] is True
    assert len(plan["proposal"]["tasks"]) == 2


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda plan: plan["dependencies"].append({"task": "write", "depends_on": "verify"}),
            "PLAN_CYCLE_DETECTED",
        ),
        (
            lambda plan: plan["tasks"][0].update({"assigned_agent_key": "missing_agent"}),
            "INVALID_AGENT_REFERENCE",
        ),
        (
            lambda plan: plan["agents"][0]["requested_permissions"].update({"shell.run": True}),
            "UNSUPPORTED_PERMISSION",
        ),
    ],
)
async def test_semantically_invalid_plans_are_never_materialized(
    client: AsyncClient,
    mutate,
    expected_code: str,  # type: ignore[no-untyped-def]
) -> None:
    proposal = valid_proposal()
    mutate(proposal)
    result = await plan_with(client, proposal, f"Invalid {expected_code}")
    assert result["run"].status == PlanningRunStatus.INVALID
    errors = result["run"].validation_result["errors"]
    assert expected_code in {error["code"] for error in errors}
    mission = (await client.get(f"/api/v1/missions/{result['mission']['id']}")).json()
    assert mission["status"] == MissionStatus.DRAFT.value
    async with get_session_factory()() as session:
        assert await count(session, Project) == 0
        assert await count(session, TaskDependency) == 0


def test_plan_limits_and_duplicate_keys_are_deterministic() -> None:
    proposal = valid_proposal()
    proposal["agents"].append(dict(proposal["agents"][0]))
    result = PlanValidator(Settings(mission_max_agents=1)).validate(
        PlanProposal.model_validate(proposal)
    )
    codes = {error.code for error in result.errors}
    assert "PLAN_LIMIT_EXCEEDED" in codes
    assert "INVALID_PLAN" in codes

    tasks = valid_proposal()
    tasks["tasks"].append({**tasks["tasks"][0], "key": "extra"})
    task_result = PlanValidator(Settings(mission_max_tasks=2)).validate(
        PlanProposal.model_validate(tasks)
    )
    assert "PLAN_LIMIT_EXCEEDED" in {error.code for error in task_result.errors}

    dependencies = valid_proposal()
    dependency_result = PlanValidator(Settings(mission_max_dependencies=0)).validate(
        PlanProposal.model_validate(dependencies)
    )
    assert "PLAN_LIMIT_EXCEEDED" in {error.code for error in dependency_result.errors}


async def test_paid_planning_gate_blocks_before_provider_call(client: AsyncClient) -> None:
    mission = await create_mission(client, "Paid Gate")
    provider = MockModelProvider(response=valid_proposal())
    provider.paid = True  # type: ignore[misc]
    async with get_session_factory()() as session:
        with pytest.raises(PaidModelCallDisabledError):
            await MissionPlanner(
                session,
                settings=Settings(allow_paid_model_calls=False),
                provider=provider,
            ).plan(UUID(mission["id"]))
    assert provider.call_count == 0
    async with get_session_factory()() as session:
        assert await count(session, PlanningRun) == 0


class ScriptedPlannerProvider(MockModelProvider):
    def __init__(self, outcomes: list[ProviderCallError | object]) -> None:
        super().__init__()
        self.outcomes = outcomes

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        outcome = self.outcomes[min(self.call_count - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, ProviderCallError):
            raise outcome
        return ModelResponse(
            output=outcome,
            usage=self.usage,
            provider=self.name,
            model=request.model,
        )


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (
            ProviderCallError(
                "MODEL_RATE_LIMIT",
                "rate limited",
                retryable=True,
                category="RATE_LIMIT",
                http_status=429,
                provider_error_code="rate_limit_exceeded",
            ),
            "PLANNER_RATE_LIMITED",
        ),
        (
            ProviderCallError(
                "MODEL_PROVIDER_TEMPORARY",
                "temporary failure",
                retryable=True,
                category="TEMPORARY_PROVIDER",
                http_status=503,
            ),
            "PLANNER_PROVIDER_TEMPORARY",
        ),
    ],
)
async def test_transient_planner_failures_exhaust_bounded_retry_budget(
    client: AsyncClient,
    provider_error: ProviderCallError,
    expected_code: str,
) -> None:
    mission = await create_mission(client, f"Transient {expected_code}")
    provider = ScriptedPlannerProvider([provider_error])
    settings = Settings(planner_provider_max_attempts=3, planner_retry_base_seconds=0)

    async with get_session_factory()() as session:
        with pytest.raises(MissionPlanningError) as raised:
            await MissionPlanner(session, settings=settings, provider=provider).plan(
                UUID(mission["id"])
            )

    assert raised.value.code == expected_code
    assert raised.value.details["retryable"] is True
    assert raised.value.details["retry_exhausted"] is True
    assert provider.call_count == 3
    async with get_session_factory()() as session:
        runs = list(await session.scalars(select(PlanningRun)))
        assert len(runs) == 1
        run = runs[0]
        assert run.provider_attempts == 3
        assert [entry["outcome"] for entry in run.retry_history] == [
            "RETRY_SCHEDULED",
            "RETRY_SCHEDULED",
            "FAILED",
        ]
        assert run.error["code"] == expected_code
        assert run.error["mission_attempt_consumed"] is False
        assert run.internal_diagnostics["latest_error"]["http_status"] in {429, 503}
        stored = await session.get(Mission, UUID(mission["id"]))
        assert stored is not None
        assert stored.status == MissionStatus.DRAFT
        assert stored.planning_attempts == 0


async def test_planner_timeout_retries_then_exposes_normalized_failure(
    client: AsyncClient,
) -> None:
    mission = await create_mission(client, "Planner timeout")
    provider = MockModelProvider(response=valid_proposal(), delay_seconds=0.05)
    settings = Settings(
        planner_request_timeout_seconds=0.001,
        planner_provider_max_attempts=2,
        planner_retry_base_seconds=0,
    )

    async with get_session_factory()() as session:
        with pytest.raises(MissionPlanningError) as raised:
            await MissionPlanner(session, settings=settings, provider=provider).plan(
                UUID(mission["id"])
            )

    assert raised.value.code == "PLANNER_TIMEOUT"
    assert raised.value.status_code == 504
    assert raised.value.details["provider_attempts"] == 2
    assert provider.call_count == 2


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (
            ProviderCallError(
                "MODEL_AUTH_ERROR", "unsafe provider text", category="AUTHENTICATION"
            ),
            "PLANNER_AUTHENTICATION_FAILED",
        ),
        (
            ProviderCallError(
                "MODEL_CONFIGURATION_ERROR",
                "unsafe provider text",
                category="CONFIGURATION",
                http_status=400,
            ),
            "PLANNER_CONFIGURATION_ERROR",
        ),
        (
            ProviderCallError(
                "MODEL_QUOTA_EXHAUSTED",
                "unsafe provider text",
                category="QUOTA",
                http_status=429,
                provider_error_code="credit_balance_exhausted",
            ),
            "PLANNER_QUOTA_EXHAUSTED",
        ),
    ],
)
async def test_deterministic_planner_provider_failures_are_not_retried(
    client: AsyncClient,
    provider_error: ProviderCallError,
    expected_code: str,
) -> None:
    mission = await create_mission(client, f"Deterministic {expected_code}")
    provider = ScriptedPlannerProvider([provider_error])
    settings = Settings(planner_provider_max_attempts=5, planner_retry_base_seconds=0)

    async with get_session_factory()() as session:
        with pytest.raises(MissionPlanningError) as raised:
            await MissionPlanner(session, settings=settings, provider=provider).plan(
                UUID(mission["id"])
            )

    assert raised.value.code == expected_code
    assert raised.value.details["retryable"] is False
    assert provider.call_count == 1
    async with get_session_factory()() as session:
        run = await session.scalar(select(PlanningRun))
        assert run is not None and run.provider_attempts == 1
        assert run.error["mission_attempt_consumed"] is False
        stored = await session.get(Mission, UUID(mission["id"]))
        assert stored is not None
        assert stored.status == MissionStatus.DRAFT
        assert stored.planning_attempts == 0
        serialized = str({"error": run.error, "diagnostics": run.internal_diagnostics})
        assert "unsafe provider text" not in serialized


async def test_planner_transient_retry_succeeds_in_same_run(client: AsyncClient) -> None:
    mission = await create_mission(client, "Planner recovery")
    provider = ScriptedPlannerProvider(
        [
            ProviderCallError(
                "MODEL_PROVIDER_TEMPORARY",
                "temporary",
                retryable=True,
                category="TEMPORARY_PROVIDER",
                http_status=503,
            ),
            valid_proposal(),
        ]
    )
    settings = Settings(planner_provider_max_attempts=3, planner_retry_base_seconds=0)

    async with get_session_factory()() as session:
        run = await MissionPlanner(session, settings=settings, provider=provider).plan(
            UUID(mission["id"])
        )

    assert run.status == PlanningRunStatus.SUCCEEDED
    assert run.provider_attempts == 2
    assert [entry["outcome"] for entry in run.retry_history] == [
        "RETRY_SCHEDULED",
        "SUCCEEDED",
    ]
    assert provider.call_count == 2
    async with get_session_factory()() as session:
        assert await count(session, PlanningRun) == 1
        stored = await session.get(Mission, UUID(mission["id"]))
        assert stored is not None and stored.planning_attempts == 1


async def test_malformed_and_schema_invalid_planner_outputs_do_not_retry(
    client: AsyncClient,
) -> None:
    malformed_mission = await create_mission(client, "Malformed response")
    malformed = ScriptedPlannerProvider(
        [
            ProviderCallError(
                "INVALID_MODEL_OUTPUT",
                "raw malformed payload",
                category="RESPONSE_VALIDATION",
                http_status=200,
                details={
                    "finish_reason": "STOP",
                    "candidate_count": 1,
                    "content_exists": True,
                    "response_shape": {
                        "type": "object",
                        "top_level_fields": ["agents", "dependencies", "project", "tasks"],
                        "text_length": 1400,
                    },
                    "validation_errors": [
                        {
                            "field": "$.tasks[0].acceptance_criteria",
                            "expected": "array",
                            "received": {"type": "string", "length": 18},
                            "error_type": "list_type",
                        }
                    ],
                    "unsafe_raw_response": "secret-must-never-be-public",
                },
            )
        ]
    )
    async with get_session_factory()() as session:
        with pytest.raises(MissionPlanningError) as raised:
            await MissionPlanner(session, provider=malformed).plan(
                UUID(malformed_mission["id"])
            )
    assert raised.value.code == "PLANNER_MALFORMED_RESPONSE"
    assert raised.value.details["provider_status"] == 200
    assert raised.value.details["finish_reason"] == "STOP"
    assert raised.value.details["validation_errors"][0]["field"] == (
        "$.tasks[0].acceptance_criteria"
    )
    assert raised.value.details["validation_errors"][0]["received"] == {
        "type": "string",
        "length": 18,
    }
    assert "unsafe_raw_response" not in raised.value.details
    assert "secret-must-never-be-public" not in str(raised.value.details)
    assert malformed.call_count == 1

    async with get_session_factory()() as session:
        stored_run = await session.get(PlanningRun, UUID(raised.value.details["planning_run_id"]))
        assert stored_run is not None
        assert stored_run.error["validation_errors"][0]["field"] == (
            "$.tasks[0].acceptance_criteria"
        )
        assert "secret-must-never-be-public" not in str(stored_run.error)
        assert "secret-must-never-be-public" not in str(stored_run.internal_diagnostics)

    schema_mission = await create_mission(client, "Schema invalid response")
    schema_provider = ScriptedPlannerProvider([{"unexpected": True}])
    async with get_session_factory()() as session:
        run = await MissionPlanner(session, provider=schema_provider).plan(
            UUID(schema_mission["id"])
        )
    assert run.status == PlanningRunStatus.INVALID
    assert run.error["code"] == "PLANNER_SCHEMA_VALIDATION_FAILED"
    assert run.error["retryable"] is False
    assert run.provider_attempts == 1
    assert schema_provider.call_count == 1


async def test_mission_context_rejects_secret_fields(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/missions",
        json={
            "title": "Unsafe context",
            "goal": "Should be rejected",
            "context": {"OPENAI_API_KEY": "never-store-secrets"},
        },
    )
    assert response.status_code == 422
    async with get_session_factory()() as session:
        assert await count(session, Mission) == 0


async def test_planning_concurrency_allows_only_one_active_run(client: AsyncClient) -> None:
    mission = await create_mission(client, "Planning Race")
    mission_id = UUID(mission["id"])

    async def invoke(delay: float):
        async with get_session_factory()() as session:
            return await MissionPlanner(
                session,
                provider=MockModelProvider(response=valid_proposal(), delay_seconds=delay),
            ).plan(mission_id)

    results = await asyncio.gather(invoke(0.1), invoke(0), return_exceptions=True)
    assert sum(isinstance(result, PlanningRun) for result in results) == 1
    assert sum(isinstance(result, MissionConflictError) for result in results) == 1
    async with get_session_factory()() as session:
        assert await count(session, PlanningRun) == 1


async def test_activation_is_atomic_idempotent_and_dependency_aware(
    client: AsyncClient,
) -> None:
    mission = await create_mission(client, "Activation Mission")
    assert (await client.post(f"/api/v1/missions/{mission['id']}/plan")).status_code == 200
    first = await client.post(f"/api/v1/missions/{mission['id']}/activate")
    second = await client.post(f"/api/v1/missions/{mission['id']}/activate")
    assert first.status_code == second.status_code == 200
    assert first.json()["project_id"] == second.json()["project_id"]
    project_id = first.json()["project_id"]

    graph = (await client.get(f"/api/v1/projects/{project_id}/graph")).json()
    states = {node["title"]: node["status"] for node in graph["nodes"]}
    assert states == {"Create project brief": "QUEUED", "Verify project brief": "CREATED"}
    blocked = next(node for node in graph["nodes"] if node["status"] == "CREATED")
    assert blocked["blocked_reason"] == "BLOCKED_BY_UNMET_DEPENDENCY"
    async with get_session_factory()() as session:
        assert await count(session, Company) == 1
        assert await count(session, Project) == 1
        assert await count(session, Agent) == 1
        assert await count(session, Task) == 2
        assert await count(session, TaskDependency) == 1
        assert await count(session, ExecutionJob) == 0


async def test_activation_concurrency_materializes_one_topology(client: AsyncClient) -> None:
    mission = await create_mission(client, "Activation Race")
    await client.post(f"/api/v1/missions/{mission['id']}/plan")
    mission_id = UUID(mission["id"])

    async def activate():  # type: ignore[no-untyped-def]
        async with get_session_factory()() as session:
            return await CompanyFactory(session).activate(mission_id)

    results = await asyncio.gather(activate(), activate())
    assert results[0].project_id == results[1].project_id
    async with get_session_factory()() as session:
        assert await count(session, Project) == 1
        assert await count(session, Task) == 2
        assert await count(session, TaskDependency) == 1


async def test_factory_failure_rolls_back_entire_topology(client: AsyncClient) -> None:
    mission = await create_mission(client, "Atomic Factory")
    assert (await client.post(f"/api/v1/missions/{mission['id']}/plan")).status_code == 200
    async with get_session_factory()() as session:
        factory = CompanyFactory(session)
        original = factory.events.create

        async def fail_on_task(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs["event_type"] == "TASK_CREATED":
                raise RuntimeError("forced factory failure")
            return await original(**kwargs)

        factory.events.create = fail_on_task  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="forced factory failure"):
            await factory.activate(UUID(mission["id"]))

    async with get_session_factory()() as session:
        assert await count(session, Company) == 0
        assert await count(session, Project) == 0
        assert await count(session, Agent) == 0
        assert await count(session, Task) == 0
        stored = await session.get(Mission, UUID(mission["id"]))
        assert stored is not None and stored.status == MissionStatus.PLAN_READY


async def test_review_gate_unlocks_dependencies_and_completes_mission(
    client: AsyncClient,
) -> None:
    mission = await create_mission(client, "Review Gate")
    await client.post(f"/api/v1/missions/{mission['id']}/plan")
    active = (await client.post(f"/api/v1/missions/{mission['id']}/activate")).json()
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    root = next(node for node in graph["nodes"] if node["status"] == "QUEUED")
    downstream = next(node for node in graph["nodes"] if node["status"] == "CREATED")

    async with get_session_factory()() as session:
        machine = TaskStateMachine(session)
        await machine.transition(UUID(root["id"]), TaskStatus.IN_PROGRESS)
        await machine.transition(UUID(root["id"]), TaskStatus.REVIEW)
    still_blocked = (await client.get(f"/api/v1/tasks/{downstream['id']}")).json()
    assert still_blocked["status"] == TaskStatus.CREATED.value

    approved = await client.post(f"/api/v1/tasks/{root['id']}/approve")
    assert approved.status_code == 200
    ready = (await client.get(f"/api/v1/tasks/{downstream['id']}")).json()
    assert ready["status"] == TaskStatus.QUEUED.value

    async with get_session_factory()() as session:
        machine = TaskStateMachine(session)
        await machine.transition(UUID(downstream["id"]), TaskStatus.IN_PROGRESS)
        await machine.transition(UUID(downstream["id"]), TaskStatus.REVIEW)
    await client.post(f"/api/v1/tasks/{downstream['id']}/approve")
    finished = (await client.get(f"/api/v1/missions/{mission['id']}")).json()
    assert finished["status"] == MissionStatus.COMPLETED.value
    project = (await client.get(f"/api/v1/projects/{active['project_id']}")).json()
    assert project["status"] == ProjectStatus.COMPLETED.value


async def test_stale_planning_recovery_is_replannable(client: AsyncClient) -> None:
    mission = await create_mission(client, "Stale Planning")
    mission_id = UUID(mission["id"])
    async with get_session_factory()() as session:
        stored = await session.get(Mission, mission_id)
        assert stored is not None
        stored.status = MissionStatus.PLANNING
        stored.planning_attempts = 1
        session.add(
            PlanningRun(
                mission_id=mission_id,
                status=PlanningRunStatus.RUNNING,
                provider="mock",
                model_alias="planner",
                resolved_model="mock",
                started_at=datetime.now(UTC) - timedelta(seconds=10),
            )
        )
        await session.commit()
    async with get_session_factory()() as session:
        recovered = await MissionPlanner(
            session, settings=Settings(planning_run_stale_seconds=1)
        ).recover_stale()
        assert recovered == 1
    detail = (await client.get(f"/api/v1/missions/{mission['id']}")).json()
    assert detail["status"] == MissionStatus.DRAFT.value


async def test_full_autonomous_mission_e2e_uses_mock_only(
    client: AsyncClient, tmp_path: Path
) -> None:
    mission = await create_mission(client, "Autonomous Mission")
    plan = await client.post(f"/api/v1/missions/{mission['id']}/plan")
    assert plan.json()["provider"] == "mock"
    active = (await client.post(f"/api/v1/missions/{mission['id']}/activate")).json()
    assert active["progress"]["queued"] == 1
    assert active["progress"]["blocked"] == 1
    async with get_session_factory()() as session:
        assert await count(session, ExecutionJob) == 0

    await client.post("/api/v1/orchestrator/resume")
    async with get_session_factory()() as session:
        await OrchestratorService(session).reconcile()
        worker = await WorkerExecutionService(session).register("phase08-e2e", 1)

    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    for expected_title in ("Create project brief", "Verify project brief"):
        async with get_session_factory()() as session:
            job = await WorkerExecutionService(session).claim(worker.id, worker.worker_key)
        assert job is not None
        async with get_session_factory()() as session:
            started = await WorkerExecutionService(session).start(job.id, worker.worker_key)
        assert started is not None and started.status == ExecutionJobStatus.RUNNING
        async with get_session_factory()() as session:
            run = await AgentRuntime(
                session, settings=settings, provider=MockModelProvider()
            ).execute(job.task_id)
        async with get_session_factory()() as session:
            assert await WorkerExecutionService(session).succeed(
                job.id, worker.worker_key, run.task_run_id
            )
        task = (await client.get(f"/api/v1/tasks/{job.task_id}")).json()
        assert task["title"] == expected_title
        assert task["status"] == TaskStatus.REVIEW.value
        assert (await client.post(f"/api/v1/tasks/{job.task_id}/approve")).status_code == 200
        async with get_session_factory()() as session:
            await OrchestratorService(session).reconcile()

    finished = (await client.get(f"/api/v1/missions/{mission['id']}")).json()
    assert finished["status"] == MissionStatus.COMPLETED.value
    assert finished["progress"]["done"] == 2
    assert (
        tmp_path / "workspaces" / active["company_id"] / active["project_id"] / "brief.txt"
    ).read_text(encoding="utf-8") == "Forge Mission project brief"
    async with get_session_factory()() as session:
        jobs = list(await session.scalars(select(ExecutionJob)))
        runs = list(await session.scalars(select(PlanningRun)))
    assert [job.status for job in jobs] == [
        ExecutionJobStatus.SUCCEEDED,
        ExecutionJobStatus.SUCCEEDED,
    ]
    assert runs[0].estimated_cost == 0


async def test_multi_dependency_waits_for_every_approved_task(client: AsyncClient) -> None:
    proposal = valid_proposal()
    proposal["tasks"] = [
        {**proposal["tasks"][0], "key": "a", "title": "Task A"},
        {**proposal["tasks"][0], "key": "b", "title": "Task B"},
        {**proposal["tasks"][1], "key": "c", "title": "Task C"},
    ]
    proposal["dependencies"] = [
        {"task": "c", "depends_on": "a"},
        {"task": "c", "depends_on": "b"},
    ]
    planned = await plan_with(client, proposal, "Multi Dependency")
    active = (await client.post(f"/api/v1/missions/{planned['mission']['id']}/activate")).json()
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    nodes = {node["title"]: node for node in graph["nodes"]}
    for title in ("Task A", "Task B"):
        async with get_session_factory()() as session:
            machine = TaskStateMachine(session)
            await machine.transition(UUID(nodes[title]["id"]), TaskStatus.IN_PROGRESS)
            await machine.transition(UUID(nodes[title]["id"]), TaskStatus.REVIEW)
        if title == "Task A":
            await client.post(f"/api/v1/tasks/{nodes[title]['id']}/approve")
            blocked = (await client.get(f"/api/v1/tasks/{nodes['Task C']['id']}")).json()
            assert blocked["status"] == TaskStatus.CREATED.value
    await client.post(f"/api/v1/tasks/{nodes['Task B']['id']}/approve")
    ready = (await client.get(f"/api/v1/tasks/{nodes['Task C']['id']}")).json()
    assert ready["status"] == TaskStatus.QUEUED.value


async def test_failed_dependency_stays_blocked_without_redis_wakeup(
    client: AsyncClient,
) -> None:
    planned = await plan_with(client, valid_proposal(), "Failed Dependency")
    active = (await client.post(f"/api/v1/missions/{planned['mission']['id']}/activate")).json()
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    root = next(node for node in graph["nodes"] if node["status"] == "QUEUED")
    downstream = next(node for node in graph["nodes"] if node["status"] == "CREATED")
    async with get_session_factory()() as session:
        await TaskStateMachine(session).transition(
            UUID(root["id"]), TaskStatus.FAILED, "Deterministic task failure."
        )
    async with get_session_factory()() as session:
        await DependencyResolver(session).reconcile()
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    blocked = next(node for node in graph["nodes"] if node["id"] == downstream["id"])
    assert blocked["status"] == TaskStatus.CREATED.value
    assert blocked["blocked_reason"] == "BLOCKED_BY_FAILED_DEPENDENCY"


async def test_request_fix_and_mission_cancellation_use_state_machine(
    client: AsyncClient,
) -> None:
    mission = await create_mission(client, "Fix and Cancel")
    await client.post(f"/api/v1/missions/{mission['id']}/plan")
    active = (await client.post(f"/api/v1/missions/{mission['id']}/activate")).json()
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    root = next(node for node in graph["nodes"] if node["status"] == "QUEUED")
    async with get_session_factory()() as session:
        machine = TaskStateMachine(session)
        await machine.transition(UUID(root["id"]), TaskStatus.IN_PROGRESS)
        await machine.transition(UUID(root["id"]), TaskStatus.REVIEW)
    fixed = await client.post(
        f"/api/v1/tasks/{root['id']}/request-fix",
        json={"feedback": "Revise the root task result before continuing."},
    )
    assert fixed.status_code == 200
    assert fixed.json()["status"] == TaskStatus.QUEUED.value

    cancelled = await client.post(f"/api/v1/missions/{mission['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == MissionStatus.CANCELLED.value
    project = (await client.get(f"/api/v1/projects/{active['project_id']}")).json()
    assert project["status"] == ProjectStatus.CANCELLED.value
    graph = (await client.get(f"/api/v1/projects/{active['project_id']}/graph")).json()
    assert {node["status"] for node in graph["nodes"]} == {TaskStatus.CANCELLED.value}
