import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.agent_runtime.builders import ContextBuilder, InstructionBuilder
from app.agent_runtime.contracts import ModelRequest, ModelResponse, ProviderCallError
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.recovery import StaleAgentRunRecovery
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import (
    AcceptanceVerificationStatus,
    AgentRunStatus,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.exceptions import AgentRuntimeDomainError, PaidModelCallDisabledError
from app.domain.models import (
    AcceptanceVerification,
    AgentRun,
    Event,
    EventOutbox,
    ModelCallRecord,
    Task,
    TaskRun,
)
from app.infrastructure.database import get_session_factory
from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def queued_domain(
    client: AsyncClient, role: str = "GENERAL"
) -> dict[str, dict[str, object]]:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"], role=role)
    task = await create_task(client, company["id"], project["id"], agent["id"])
    await transition_task(client, task["id"], "QUEUED")
    return {"company": company, "project": project, "agent": agent, "task": task}


async def test_capability_route_failure_is_normalized_before_worker_boundary(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client, role="DEVELOPER")
    settings = Settings(
        model_catalog=[
            {
                "alias": "planner",
                "provider": "gemini",
                "model": "gemini-test",
                "tier": "FREE",
                "capabilities": ["TEXT", "REASONING", "STRUCTURED_OUTPUT"],
                "enabled": True,
                "paid": False,
                "max_call_cost": 0,
            }
        ]
    )
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings).execute(
                UUID(str(domain["task"]["id"]))
            )
    assert raised.value.code == "MODEL_CAPABILITY_UNAVAILABLE"
    assert "required capabilities" in raised.value.message


async def test_mock_execution_end_to_end(client: AsyncClient) -> None:
    domain = await queued_domain(client, role="RESEARCHER")
    task_id = str(domain["task"]["id"])

    response = await client.post(f"/api/v1/tasks/{task_id}/execute", json={})

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "SUCCEEDED"
    assert run["provider"] == "mock"
    assert run["model_alias"] == "default"
    assert run["response"]["status"] == "completed"
    assert run["response"]["output"]["result"] == "deterministic-mock-output"
    assert run["total_tokens"] == 30
    assert run["cached_tokens"] == 2
    assert float(run["estimated_cost"]) == 0
    assert run["request"]["stored"] is False
    assert "system_prompt" not in run["request"]
    assert isinstance(run["request"]["tool_names"], list)
    assert len(run["request"]["system_prompt_sha256"]) == 64

    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    task_runs = (await client.get(f"/api/v1/tasks/{task_id}/runs?limit=1")).json()
    agent_runs = (await client.get(f"/api/v1/tasks/{task_id}/agent-runs")).json()
    fetched = (await client.get(f"/api/v1/agent-runs/{run['id']}")).json()
    stats = (await client.get("/api/v1/agent-runs/stats")).json()
    recent = (await client.get("/api/v1/agent-runs?limit=1")).json()

    assert task["status"] == "REVIEW"
    assert task_runs[0]["status"] == "SUCCEEDED"
    assert task_runs[0]["output"] == run["response"]
    assert agent_runs[0]["id"] == run["id"] == fetched["id"]
    assert stats["total"] == 1
    assert stats["by_status"]["SUCCEEDED"] == 1
    assert stats["total_tokens"] == 30
    assert stats["cached_tokens"] == 2
    assert recent[0]["task_title"] == domain["task"]["title"]

    async with get_session_factory()() as session:
        events = list(await session.scalars(select(Event).where(Event.task_id == task_id)))
        event_types = {event.type for event in events}
        assert {"AGENT_RUN_STARTED", "AGENT_RUN_SUCCEEDED"}.issubset(event_types)
        assert all(str(event.correlation_id) == task_id for event in events)
        assert await session.scalar(select(func.count()).select_from(EventOutbox)) == len(
            list(await session.scalars(select(Event)))
        )


async def test_durable_acceptance_evidence_skips_an_unnecessary_model_call(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client)
    task_id = str(domain["task"]["id"])
    first = await client.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert first.status_code == 200

    async with get_session_factory()() as session:
        task_run = await session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert task_run is not None
        session.add_all(
            [
                AcceptanceVerification(
                    task_id=task_run.task_id,
                    task_run_id=task_run.id,
                    criterion_index=index,
                    criterion=criterion,
                    iteration=task_run.iteration,
                    status=AcceptanceVerificationStatus.PASSED,
                    verifier="DETERMINISTIC_TEST",
                    evidence_summary="Persisted deterministic evidence passed.",
                    execution_ids=[],
                )
                for index, criterion in enumerate(domain["task"]["acceptance_criteria"])
            ]
        )
        await session.commit()

    await transition_task(client, task_id, "FIX_REQUIRED", "Re-inspect persisted evidence")
    await transition_task(client, task_id, "QUEUED")

    class FailIfCalled(MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError(f"Unexpected model request: {request.model}")

    async with get_session_factory()() as session:
        completed = await AgentRuntime(
            session,
            settings=Settings(model_provider="mock"),
            provider=FailIfCalled(),
        ).execute(UUID(task_id))
        assert completed.provider == "deterministic"
        assert completed.model_id == "persisted-acceptance-evidence"
        assert "no model call" in completed.selection_reason.lower()

    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ModelCallRecord)) == 1


async def test_concurrent_execute_creates_exactly_one_agent_run(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    task_id = str(domain["task"]["id"])

    first, second = await asyncio.gather(
        client.post(f"/api/v1/tasks/{task_id}/execute", json={}),
        client.post(f"/api/v1/tasks/{task_id}/execute", json={}),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    async with get_session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert await session.scalar(select(func.count()).select_from(TaskRun)) == 1


async def test_unknown_alias_does_not_start_task(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    task_id = str(domain["task"]["id"])

    response = await client.post(
        f"/api/v1/tasks/{task_id}/execute", json={"model_alias": "missing"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MODEL_ALIAS_NOT_FOUND"
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["status"] == "QUEUED"


@pytest.mark.parametrize(
    ("provider", "expected_code"),
    [
        (
            MockModelProvider(error=ProviderCallError("MODEL_RATE_LIMIT", "Rate limit reached")),
            "MODEL_RATE_LIMIT",
        ),
        (MockModelProvider(response={"status": "not-completed"}), "INVALID_MODEL_OUTPUT"),
        (
            MockModelProvider(error=ProviderCallError("UNKNOWN", "Untrusted provider detail")),
            "AGENT_RUNTIME_ERROR",
        ),
    ],
)
async def test_failures_are_normalized_and_persisted(
    client: AsyncClient, provider: MockModelProvider, expected_code: str
) -> None:
    domain = await queued_domain(client)
    task_id = domain["task"]["id"]
    settings = Settings(model_provider="mock", model_request_timeout_seconds=1)

    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=provider).execute(task_id)
        assert raised.value.code == expected_code

    async with get_session_factory()() as session:
        run = await session.scalar(select(AgentRun))
        task = await session.get(Task, task_id)
        task_run = await session.scalar(select(TaskRun))
        assert run is not None and run.status == AgentRunStatus.FAILED
        assert run.error_code == expected_code
        assert task is not None and task.status == TaskStatus.FAILED
        assert task_run is not None and task_run.status == TaskRunStatus.FAILED
        assert task_run.error["code"] == expected_code


async def test_model_timeout_retries_within_same_task_run(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    calls = 0

    class SlowProvider(MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls
            calls += 1
            return await super().generate(request)

    provider = SlowProvider(delay_seconds=0.1)
    settings = Settings(
        model_provider="mock",
        model_request_timeout_seconds=0.01,
        model_transient_max_attempts=3,
        model_retry_base_seconds=0,
    )
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=provider).execute(
                domain["task"]["id"]
            )
    assert raised.value.code == "MODEL_TIMEOUT"
    assert calls == 3
    async with get_session_factory()() as session:
        run = await session.scalar(select(AgentRun))
        task_runs = list(await session.scalars(select(TaskRun)))
        assert run is not None
        assert len(run.request["retry_history"]) == 2
        assert len(task_runs) == 1


async def test_transient_model_failure_recovers_without_new_task_iteration(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client)

    class RecoveringProvider(MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            self.call_count += 1
            self.requests.append(request)
            if self.call_count == 1:
                raise ProviderCallError("MODEL_RATE_LIMIT", "Rate limit reached")
            return ModelResponse(
                output={
                    "status": "completed",
                    "summary": "Recovered.",
                    "output": {},
                    "notes": [],
                },
                usage=self.usage,
            )

    provider = RecoveringProvider()
    settings = Settings(model_retry_base_seconds=0, model_transient_max_attempts=3)
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            domain["task"]["id"]
        )
    assert run.status == AgentRunStatus.SUCCEEDED
    assert provider.call_count == 2
    async with get_session_factory()() as session:
        task = await session.get(Task, UUID(domain["task"]["id"]))
        task_runs = list(await session.scalars(select(TaskRun)))
        persisted = await session.get(AgentRun, run.id)
        assert task is not None and task.iteration == 1 and task.status == TaskStatus.REVIEW
        assert len(task_runs) == 1
        assert persisted is not None
        assert persisted.request["retry_history"][0]["outcome"] == "RETRY_SCHEDULED"


def test_default_model_timeout_allows_frontier_reasoning_latency() -> None:
    assert Settings.model_fields["model_request_timeout_seconds"].default == 120.0


async def test_paid_provider_is_blocked_before_any_call() -> None:
    calls = 0

    class PaidProvider:
        name = "openai"
        paid = True

        async def generate(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls
            calls += 1
            raise AssertionError(request)

    settings = Settings(model_provider="openai", allow_paid_model_calls=False)
    async with get_session_factory()() as session:
        with pytest.raises(PaidModelCallDisabledError):
            await AgentRuntime(session, settings=settings, provider=PaidProvider()).execute(uuid4())
    assert calls == 0


async def test_missing_openai_key_is_rejected_before_any_call() -> None:
    calls = 0

    class PaidProvider:
        name = "openai"
        paid = True

        async def generate(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls
            calls += 1
            raise AssertionError(request)

    settings = Settings(
        model_provider="openai",
        allow_paid_model_calls=True,
        openai_api_key=None,
    )
    async with get_session_factory()() as session:
        with pytest.raises(AgentRuntimeDomainError) as raised:
            await AgentRuntime(session, settings=settings, provider=PaidProvider()).execute(uuid4())
    assert raised.value.code == "MODEL_AUTH_ERROR"
    assert calls == 0


async def test_context_scope_and_instruction_layers_exclude_unrelated_company(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client)
    unrelated = await create_company(client, slug="unrelated-company")

    async with get_session_factory()() as session:
        context = await ContextBuilder(session).build(domain["task"]["id"])
    instructions = InstructionBuilder().build(context)

    serialized = context.model_dump_json()
    assert str(domain["company"]["id"]) in serialized
    assert str(unrelated["id"]) not in serialized
    assert instructions.runtime_role == "GENERAL"
    assert "no tools" in instructions.system_prompt
    assert "structured result" in instructions.system_prompt.lower()


async def test_provider_key_never_enters_run_event_or_api_data(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    fake_key = "phase05-test-secret-value"
    settings = Settings(
        model_provider="mock",
        openai_api_key=SecretStr(fake_key),
        store_model_inputs=False,
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=MockModelProvider()).execute(
            domain["task"]["id"]
        )

    api_data = (await client.get(f"/api/v1/agent-runs/{run.id}")).json()
    async with get_session_factory()() as session:
        events = list(await session.scalars(select(Event)))
    persisted = json.dumps(
        {"api": api_data, "events": [event.details for event in events]}, default=str
    )
    assert fake_key not in persisted
    assert "system_prompt" not in api_data["request"]


async def test_stale_running_agent_run_recovery(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    task_id = domain["task"]["id"]
    await transition_task(client, task_id, "IN_PROGRESS")
    async with get_session_factory()() as session:
        task_run = await session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert task_run is not None
        session.add(
            AgentRun(
                task_run_id=task_run.id,
                task_id=task_run.task_id,
                agent_id=task_run.agent_id,
                status=AgentRunStatus.RUNNING,
                provider="mock",
                model_alias="default",
                model_id="mock-test",
                request={"stored": False},
                started_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        await session.commit()

    settings = Settings(agent_run_stale_seconds=1)
    async with get_session_factory()() as session:
        assert await StaleAgentRunRecovery(session, settings).recover() == 1

    async with get_session_factory()() as session:
        run = await session.scalar(select(AgentRun))
        task = await session.get(Task, task_id)
        assert run is not None and run.status == AgentRunStatus.FAILED
        assert run.error_code == "AGENT_RUNTIME_ERROR"
        assert task is not None and task.status == TaskStatus.FAILED
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.status == AgentRunStatus.RUNNING)
            )
            == 0
        )
