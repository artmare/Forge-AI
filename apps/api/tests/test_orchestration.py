import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select

from app.agent_runtime.efficiency import EfficientRuntimeService
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.enums import (
    AgentRunStatus,
    AgentStatus,
    ExecutionJobStatus,
    ExecutionPhase,
    TaskRunStatus,
    TaskStatus,
    WorkerStatus,
)
from app.domain.models import Agent, AgentRun, Event, ExecutionJob, Task, TaskRun, WorkerNode
from app.infrastructure.database import get_session_factory
from app.orchestration.orchestrator import OrchestratorService
from app.orchestration.recovery import OrchestrationRecoveryService
from app.orchestration.runtime_control import RuntimeControlService
from app.orchestration.worker_service import WorkerExecutionService
from app.services.task_state_machine import TaskStateMachine
from tests.helpers import create_agent, create_company, create_project, create_task, transition_task


async def queued_domain(
    client: AsyncClient,
    *,
    suffix: str = "orchestration",
    agent_id: str | None = None,
    permissions: dict[str, bool] | None = None,
) -> dict[str, dict]:
    company = await create_company(client, f"company-{suffix}")
    project = await create_project(client, company["id"])
    agent = (
        await create_agent(client, company["id"], permissions=permissions)
        if agent_id is None
        else (await client.get(f"/api/v1/agents/{agent_id}")).json()
    )
    task = await create_task(client, company["id"], project["id"], agent["id"])
    task = await transition_task(client, task["id"], "QUEUED")
    return {"company": company, "project": project, "agent": agent, "task": task}


async def enable_and_reconcile() -> None:
    async with get_session_factory()() as session:
        await RuntimeControlService(session).set_enabled(True)
        await OrchestratorService(session).reconcile()


async def register_worker(key: str, concurrency: int = 1) -> WorkerNode:
    async with get_session_factory()() as session:
        return await WorkerExecutionService(session).register(key, concurrency)


async def claim(worker_id: UUID, key: str, settings: Settings | None = None):
    async with get_session_factory()() as session:
        return await WorkerExecutionService(session, settings).claim(worker_id, key)


async def test_paused_upgrade_default_and_idempotent_resume(client: AsyncClient) -> None:
    domain = await queued_domain(client)
    task_id = UUID(domain["task"]["id"])

    async with get_session_factory()() as session:
        paused = await OrchestratorService(session).reconcile()
        assert paused.paused is True
        assert await session.scalar(select(func.count(ExecutionJob.id))) == 0

    response = await client.post("/api/v1/orchestrator/resume")
    assert response.status_code == 200
    assert response.json()["autonomy_enabled"] is True

    async with get_session_factory()() as session:
        await OrchestratorService(session).reconcile()
        jobs = list(
            await session.scalars(select(ExecutionJob).where(ExecutionJob.task_id == task_id))
        )
    assert len(jobs) == 1
    assert jobs[0].status == ExecutionJobStatus.PENDING


async def test_two_worker_claim_race_has_one_winner(client: AsyncClient) -> None:
    await queued_domain(client, suffix="claim-race")
    await enable_and_reconcile()
    first = await register_worker("worker-race-a")
    second = await register_worker("worker-race-b")

    results = await asyncio.gather(
        claim(first.id, first.worker_key),
        claim(second.id, second.worker_key),
    )

    assert sum(result is not None for result in results) == 1
    async with get_session_factory()() as session:
        assert (
            await session.scalar(
                select(func.count(ExecutionJob.id)).where(
                    ExecutionJob.status == ExecutionJobStatus.CLAIMED
                )
            )
            == 1
        )


async def test_global_and_agent_concurrency_are_database_coordinated(
    client: AsyncClient,
) -> None:
    first = await queued_domain(client, suffix="capacity-a")
    second_agent = await create_agent(client, first["company"]["id"], role="QA")
    second_task = await create_task(
        client,
        first["company"]["id"],
        first["project"]["id"],
        second_agent["id"],
        title="Second task",
    )
    await transition_task(client, second_task["id"], "QUEUED")
    same_agent_task = await create_task(
        client,
        first["company"]["id"],
        first["project"]["id"],
        first["agent"]["id"],
        title="Same agent task",
    )
    await transition_task(client, same_agent_task["id"], "QUEUED")
    await enable_and_reconcile()
    worker_a = await register_worker("worker-capacity-a")
    worker_b = await register_worker("worker-capacity-b")
    one_at_a_time = Settings(forge_max_concurrent_tasks=1)

    results = await asyncio.gather(
        claim(worker_a.id, worker_a.worker_key, one_at_a_time),
        claim(worker_b.id, worker_b.worker_key, one_at_a_time),
    )
    assert sum(result is not None for result in results) == 1

    async with get_session_factory()() as session:
        claimed = await session.scalar(
            select(ExecutionJob).where(ExecutionJob.status == ExecutionJobStatus.CLAIMED)
        )
        assert claimed is not None
        same_agent_executing = await session.scalar(
            select(func.count(ExecutionJob.id)).where(
                ExecutionJob.agent_id == claimed.agent_id,
                ExecutionJob.status.in_((ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)),
            )
        )
        assert same_agent_executing == 1


async def test_one_task_per_agent_leaves_second_job_pending(client: AsyncClient) -> None:
    domain = await queued_domain(client, suffix="same-agent")
    second_task = await create_task(
        client,
        domain["company"]["id"],
        domain["project"]["id"],
        domain["agent"]["id"],
        title="Second same-agent task",
    )
    await transition_task(client, second_task["id"], "QUEUED")
    await enable_and_reconcile()
    first_worker = await register_worker("worker-same-agent-a")
    second_worker = await register_worker("worker-same-agent-b")

    first_job = await claim(first_worker.id, first_worker.worker_key)
    second_job = await claim(second_worker.id, second_worker.worker_key)

    assert first_job is not None
    assert second_job is None
    async with get_session_factory()() as session:
        pending = await session.scalar(
            select(func.count(ExecutionJob.id)).where(
                ExecutionJob.agent_id == UUID(domain["agent"]["id"]),
                ExecutionJob.status == ExecutionJobStatus.PENDING,
            )
        )
    assert pending == 1


async def test_job_claim_orders_priority_then_age(client: AsyncClient) -> None:
    company = await create_company(client, "company-priority")
    project = await create_project(client, company["id"])
    low_agent = await create_agent(client, company["id"], role="LOW")
    critical_agent = await create_agent(client, company["id"], role="CRITICAL")
    low_task = await create_task(
        client,
        company["id"],
        project["id"],
        low_agent["id"],
        title="Older low priority",
        priority="LOW",
    )
    critical_task = await create_task(
        client,
        company["id"],
        project["id"],
        critical_agent["id"],
        title="Newer critical priority",
        priority="CRITICAL",
    )
    await transition_task(client, low_task["id"], "QUEUED")
    await transition_task(client, critical_task["id"], "QUEUED")
    await enable_and_reconcile()
    worker = await register_worker("worker-priority")

    selected = await claim(worker.id, worker.worker_key)

    assert selected is not None
    assert selected.task_id == UUID(critical_task["id"])


async def test_autonomous_worker_path_executes_tools_without_manual_endpoint(
    client: AsyncClient, tmp_path: Path
) -> None:
    domain = await queued_domain(
        client,
        suffix="autonomous-e2e",
        permissions={
            "filesystem.list": True,
            "filesystem.read": True,
            "filesystem.write": True,
        },
    )
    await enable_and_reconcile()
    worker = await register_worker("worker-autonomous")
    job = await claim(worker.id, worker.worker_key)
    assert job is not None
    async with get_session_factory()() as session:
        started = await WorkerExecutionService(session).start(job.id, worker.worker_key)
    assert started is not None and started.status == ExecutionJobStatus.RUNNING
    async with get_session_factory()() as session:
        await RuntimeControlService(session).set_enabled(False)

    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "autonomous.txt", "content": "Phase 07"},
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.read",
                "arguments": {"path": "autonomous.txt"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Autonomous execution completed.",
                    "output": {"content": "Phase 07"},
                    "notes": [],
                },
            },
        ]
    )
    settings = Settings(tool_workspace_root=str(tmp_path / "workspaces"))
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(domain["task"]["id"])
        )
    async with get_session_factory()() as session:
        succeeded = await WorkerExecutionService(session).succeed(
            job.id, worker.worker_key, run.task_run_id
        )
    assert succeeded is True

    task = (await client.get(f"/api/v1/tasks/{domain['task']['id']}")).json()
    agent = (await client.get(f"/api/v1/agents/{domain['agent']['id']}")).json()
    jobs = (await client.get(f"/api/v1/execution-jobs?task_id={task['id']}")).json()
    assert task["status"] == TaskStatus.REVIEW.value
    assert agent["status"] == AgentStatus.IDLE.value
    assert jobs[0]["status"] == ExecutionJobStatus.SUCCEEDED.value
    assert jobs[0]["task_run_id"] == str(run.task_run_id)
    assert (
        tmp_path
        / "workspaces"
        / domain["company"]["id"]
        / domain["project"]["id"]
        / "autonomous.txt"
    ).read_text(encoding="utf-8") == "Phase 07"

    async with get_session_factory()() as session:
        events = list(
            await session.scalars(select(Event).where(Event.task_id == UUID(domain["task"]["id"])))
        )
    event_types = {event.type for event in events}
    assert {
        "EXECUTION_JOB_CREATED",
        "EXECUTION_JOB_CLAIMED",
        "EXECUTION_JOB_STARTED",
        "EXECUTION_JOB_SUCCEEDED",
        "TOOL_CALL_SUCCEEDED",
    }.issubset(event_types)
    execution_correlations = {
        event.correlation_id for event in events if event.type.startswith("EXECUTION_JOB_")
    }
    assert len(execution_correlations) == 1


async def test_lease_renewal_and_pre_execution_recovery(client: AsyncClient) -> None:
    await queued_domain(client, suffix="lease-retry")
    await enable_and_reconcile()
    worker = await register_worker("worker-lease")
    settings = Settings(job_lease_seconds=30, job_retry_base_seconds=0)
    job = await claim(worker.id, worker.worker_key, settings)
    assert job is not None
    original_expiry = job.lease_expires_at

    async with get_session_factory()() as session:
        renewed = await WorkerExecutionService(session, settings).renew_lease(
            job.id, worker.worker_key
        )
    assert renewed is True
    async with get_session_factory()() as session:
        current = await session.get(ExecutionJob, job.id)
        assert current is not None and current.lease_expires_at >= original_expiry
        current.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with get_session_factory()() as session:
        result = await OrchestrationRecoveryService(session, settings).recover()
    assert result["expired_jobs"] == 1
    async with get_session_factory()() as session:
        recovered = await session.get(ExecutionJob, job.id)
    assert recovered is not None
    assert recovered.status == ExecutionJobStatus.PENDING
    assert recovered.last_error["code"] == "LEASE_EXPIRED"


async def test_worker_persists_phases_and_normalized_unclassified_failure(
    client: AsyncClient,
) -> None:
    await queued_domain(client, suffix="durable-diagnostics")
    await enable_and_reconcile()
    worker = await register_worker("worker-diagnostics")
    job = await claim(worker.id, worker.worker_key)
    assert job is not None
    async with get_session_factory()() as session:
        service = WorkerExecutionService(session)
        started = await service.start(job.id, worker.worker_key)
        assert started is not None
    async with get_session_factory()() as session:
        service = WorkerExecutionService(session)
        assert await service.update_phase(
            job.id,
            worker.worker_key,
            ExecutionPhase.ENVIRONMENT_SETUP,
            {
                "repository_initialized": True,
                "working_tree": {"changed_files_count": 2},
                "secret": "must-not-persist",
            },
        )
        assert await service.update_phase(
            job.id, worker.worker_key, ExecutionPhase.AGENT_START
        )
        assert await service.fail(
            job.id,
            worker.worker_key,
            "UNCLASSIFIED_RUNTIME_FAILURE",
            "Unexpected RuntimeError in the agent worker runtime.",
        )
    async with get_session_factory()() as session:
        failed = await session.get(ExecutionJob, job.id)
        assert failed is not None
        assert failed.phase == ExecutionPhase.AGENT_START
        assert [entry["phase"] for entry in failed.phase_history] == [
            "PREPARING",
            "ENVIRONMENT_SETUP",
            "AGENT_START",
        ]
        assert failed.environment_state == {
            "repository_initialized": True,
            "working_tree": {"changed_files_count": 2},
        }
        assert failed.working_tree_state == {"changed_files_count": 2}
        assert failed.failure_evidence["code"] == "UNCLASSIFIED_RUNTIME_FAILURE"
        assert failed.failure_evidence["category"] == "UNCLASSIFIED_RUNTIME_FAILURE"
        assert failed.failure_evidence["phase"] == "AGENT_START"


async def test_worker_classifies_model_capability_failure(
    client: AsyncClient,
) -> None:
    await queued_domain(client, suffix="capability-diagnostics")
    await enable_and_reconcile()
    worker = await register_worker("worker-capability-diagnostics")
    job = await claim(worker.id, worker.worker_key)
    assert job is not None
    async with get_session_factory()() as session:
        started = await WorkerExecutionService(session).start(job.id, worker.worker_key)
        assert started is not None
    async with get_session_factory()() as session:
        assert await WorkerExecutionService(session).fail(
            job.id,
            worker.worker_key,
            "MODEL_CAPABILITY_UNAVAILABLE",
            "No enabled model satisfies the required capabilities",
        )
    async with get_session_factory()() as session:
        failed = await session.get(ExecutionJob, job.id)
        assert failed is not None
        assert failed.failure_evidence["code"] == "MODEL_CAPABILITY_UNAVAILABLE"
        assert failed.failure_evidence["category"] == "PROVIDER_CAPABILITY_MISMATCH"


async def test_worker_classifies_task_input_budget_and_records_rollover_diagnostics(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client, suffix="input-budget-diagnostics")
    await enable_and_reconcile()
    worker = await register_worker("worker-input-budget-diagnostics")
    job = await claim(worker.id, worker.worker_key)
    assert job is not None
    async with get_session_factory()() as session:
        task = await session.get(Task, UUID(domain["task"]["id"]))
        assert task is not None
        budget = await EfficientRuntimeService(session, Settings()).enforce_budget(task)
        budget.consumed_input_tokens = 252_613
        budget.consumed_cached_tokens = 154_141
        budget.consumed_model_calls = 13
        await session.commit()
    async with get_session_factory()() as session:
        assert await WorkerExecutionService(session).start(job.id, worker.worker_key)
    async with get_session_factory()() as session:
        assert await WorkerExecutionService(session).fail(
            job.id,
            worker.worker_key,
            "MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED",
            "Task stopped for human intervention: MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED",
        )
    async with get_session_factory()() as session:
        failed = await session.get(ExecutionJob, job.id)
        assert failed is not None
        assert failed.failure_evidence["category"] == "TASK_BUDGET_EXHAUSTION"
        diagnostics = failed.failure_evidence["budget_diagnostics"]
        assert diagnostics["input_tokens_consumed"] == 252_613
        assert diagnostics["cached_input_tokens"] == 154_141
        assert diagnostics["remaining_input_tokens"] == 0
        assert diagnostics["rollover_count"] == 0
        assert diagnostics["rollover_disposition"] == "NO_ROLLOVER_RECORDED_BEFORE_STOP"
    inspection = await client.get(
        f"/api/v1/tasks/{domain['task']['id']}/inspection"
    )
    assert inspection.status_code == 200
    failure = inspection.json()["failure"]
    assert failure["category"] == "TASK_BUDGET_EXHAUSTION"
    assert failure["budget_diagnostics"]["input_tokens_consumed"] == 252_613
    assert failure["budget_diagnostics"]["last_request_input_tokens"] == 0


async def test_recovery_preserves_manual_run_with_active_agent_turn(client: AsyncClient) -> None:
    domain = await queued_domain(client, suffix="manual-active-agent-run")
    task_id = UUID(domain["task"]["id"])
    async with get_session_factory()() as session:
        await TaskStateMachine(session).transition(task_id, TaskStatus.IN_PROGRESS)
        task_run = await session.scalar(
            select(TaskRun).where(
                TaskRun.task_id == task_id,
                TaskRun.status == TaskRunStatus.STARTED,
            )
        )
        assert task_run is not None
        session.add(
            AgentRun(
                task_run_id=task_run.id,
                task_id=task_id,
                agent_id=task_run.agent_id,
                status=AgentRunStatus.RUNNING,
                provider="mock",
                model_alias="default",
                model_id="mock-deterministic-v1",
                request={"stored": False},
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()
        result = await OrchestrationRecoveryService(session).recover()
        task = await session.get(Task, task_id)
        assert result["orphaned_task_runs"] == 0
        assert task is not None and task.status == TaskStatus.IN_PROGRESS


async def test_recovery_gives_manual_turn_boundaries_a_stale_grace_period(
    client: AsyncClient,
) -> None:
    domain = await queued_domain(client, suffix="manual-turn-grace")
    task_id = UUID(domain["task"]["id"])
    async with get_session_factory()() as session:
        await TaskStateMachine(session).transition(task_id, TaskStatus.IN_PROGRESS)
        result = await OrchestrationRecoveryService(
            session, Settings(agent_run_stale_seconds=300)
        ).recover()
        task = await session.get(Task, task_id)
        assert result["orphaned_task_runs"] == 0
        assert task is not None and task.status == TaskStatus.IN_PROGRESS
        task_run = await session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert task_run is not None
        task_run.started_at = datetime.now(UTC) - timedelta(seconds=2)
        await session.commit()
        result = await OrchestrationRecoveryService(
            session, Settings(agent_run_stale_seconds=1)
        ).recover()
        task = await session.get(Task, task_id)
        assert result["orphaned_task_runs"] == 1
        assert task is not None and task.status == TaskStatus.FAILED


async def test_worker_crash_recovery_removes_zombies(client: AsyncClient) -> None:
    domain = await queued_domain(client, suffix="worker-crash")
    await enable_and_reconcile()
    worker = await register_worker("worker-crash")
    settings = Settings(worker_stale_seconds=1, job_retry_base_seconds=0)
    job = await claim(worker.id, worker.worker_key, settings)
    assert job is not None
    async with get_session_factory()() as session:
        assert await WorkerExecutionService(session, settings).start(job.id, worker.worker_key)
    async with get_session_factory()() as session:
        await TaskStateMachine(session).transition(
            UUID(domain["task"]["id"]), TaskStatus.IN_PROGRESS
        )
        current_job = await session.get(ExecutionJob, job.id)
        current_worker = await session.get(WorkerNode, worker.id)
        assert current_job is not None and current_worker is not None
        current_job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        current_worker.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=2)
        await session.commit()

    async with get_session_factory()() as session:
        result = await OrchestrationRecoveryService(session, settings).recover()
    assert result["expired_jobs"] == 1
    assert result["stale_workers"] == 1

    async with get_session_factory()() as session:
        current_job = await session.get(ExecutionJob, job.id)
        current_worker = await session.get(WorkerNode, worker.id)
        current_agent = await session.get(Agent, UUID(domain["agent"]["id"]))
        current_run = await session.scalar(
            select(TaskRun).where(TaskRun.task_id == UUID(domain["task"]["id"]))
        )
    task = (await client.get(f"/api/v1/tasks/{domain['task']['id']}")).json()
    assert current_job is not None and current_job.status == ExecutionJobStatus.FAILED
    assert current_job.last_error["code"] == "WORKER_LOST"
    assert current_worker is not None and current_worker.status == WorkerStatus.OFFLINE
    assert current_agent is not None and current_agent.status == AgentStatus.IDLE
    assert current_run is not None and current_run.status == TaskRunStatus.FAILED
    assert task["status"] == TaskStatus.FAILED.value


async def test_pause_cancellation_and_manual_execution_conflict(client: AsyncClient) -> None:
    domain = await queued_domain(client, suffix="pause-cancel-manual")
    await enable_and_reconcile()
    await client.post("/api/v1/orchestrator/pause")
    worker = await register_worker("worker-paused")
    assert await claim(worker.id, worker.worker_key) is None

    await client.post("/api/v1/orchestrator/resume")
    manual = await client.post(
        f"/api/v1/tasks/{domain['task']['id']}/execute", json={"model_alias": "default"}
    )
    assert manual.status_code == 200
    async with get_session_factory()() as session:
        jobs = list(
            await session.scalars(
                select(ExecutionJob).where(ExecutionJob.task_id == UUID(domain["task"]["id"]))
            )
        )
        run_count = await session.scalar(
            select(func.count(TaskRun.id)).where(TaskRun.task_id == UUID(domain["task"]["id"]))
        )
    assert run_count == 1
    assert all(job.status == ExecutionJobStatus.CANCELLED for job in jobs)

    conflict = await queued_domain(client, suffix="manual-claimed-conflict")
    await enable_and_reconcile()
    claiming_worker = await register_worker("worker-manual-conflict")
    claimed = await claim(claiming_worker.id, claiming_worker.worker_key)
    assert claimed is not None
    rejected = await client.post(
        f"/api/v1/tasks/{conflict['task']['id']}/execute",
        json={"model_alias": "default"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "EXECUTION_CONFLICT"

    cancellation = await queued_domain(client, suffix="pending-cancellation")
    await enable_and_reconcile()
    await transition_task(client, cancellation["task"]["id"], "CANCELLED")
    async with get_session_factory()() as session:
        await OrchestratorService(session).reconcile()
        cancelled_job = await session.scalar(
            select(ExecutionJob).where(ExecutionJob.task_id == UUID(cancellation["task"]["id"]))
        )
    assert cancelled_job is not None and cancelled_job.status == ExecutionJobStatus.CANCELLED


async def test_operational_apis_expose_safe_state(client: AsyncClient) -> None:
    await queued_domain(client, suffix="ops-api")
    await client.post("/api/v1/orchestrator/resume")
    worker = await register_worker("worker-ops", concurrency=2)

    status = await client.get("/api/v1/orchestrator/status")
    workers = await client.get("/api/v1/workers")
    worker_detail = await client.get(f"/api/v1/workers/{worker.id}")
    jobs = await client.get("/api/v1/execution-jobs?status=PENDING")

    assert status.status_code == 200
    assert status.json()["autonomy_enabled"] is True
    assert status.json()["orchestrator_online"] is True
    assert workers.status_code == 200 and workers.json()[0]["worker_key"] == "worker-ops"
    assert worker_detail.json()["concurrency"] == 2
    assert jobs.status_code == 200 and len(jobs.json()) == 1
    assert "metadata" not in worker_detail.json()
