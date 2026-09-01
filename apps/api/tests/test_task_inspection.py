from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from httpx import AsyncClient

from app.domain.enums import (
    AcceptanceVerificationStatus,
    AgentRunStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    QADecision,
    TaskReviewDecision,
    TaskRunStatus,
    TaskStatus,
    ToolCallStatus,
)
from app.domain.models import (
    AcceptanceVerification,
    AgentRun,
    DevelopmentExecution,
    QAResult,
    Task,
    TaskDependency,
    TaskReview,
    TaskRun,
    ToolCall,
)
from app.infrastructure.database import get_session_factory
from tests.helpers import create_agent, create_company, create_project


async def test_failed_task_inspection_aggregates_iterations_and_redacts_secrets(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"inspection-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    developer = await create_agent(client, company["id"], role="DEVELOPER")
    failed_response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Implement vertical slice",
            "description": "Build and verify the bounded product slice.",
            "priority": "CRITICAL",
            "acceptance_criteria": ["npm test exits 0", "Side panel is accessible"],
            "max_iterations": 4,
        },
    )
    blocked_response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Add deterministic tests",
            "acceptance_criteria": ["Tests exist"],
            "max_iterations": 2,
        },
    )
    assert failed_response.status_code == blocked_response.status_code == 201
    failed_id = UUID(failed_response.json()["id"])
    blocked_id = UUID(blocked_response.json()["id"])
    agent_id = UUID(developer["id"])
    company_id = UUID(company["id"])
    project_id = UUID(project["id"])
    secret = "sk-inspection-secret-value"
    now = datetime.now(UTC)

    async with get_session_factory()() as session:
        failed = await session.get(Task, failed_id)
        assert failed is not None
        failed.status = TaskStatus.FAILED
        failed.iteration = 4
        failed.started_at = now - timedelta(minutes=4)
        failed.completed_at = now
        session.add(TaskDependency(task_id=blocked_id, depends_on_task_id=failed_id))
        for iteration in range(1, 5):
            started = now - timedelta(minutes=5 - iteration)
            run = TaskRun(
                task_id=failed_id,
                agent_id=agent_id,
                iteration=iteration,
                status=TaskRunStatus.FAILED,
                input={},
                error={"reason": "Deterministic QA found blocking issues."},
                started_at=started,
                completed_at=started + timedelta(seconds=10),
            )
            session.add(run)
            await session.flush()
            agent_run = AgentRun(
                task_run_id=run.id,
                task_id=failed_id,
                agent_id=agent_id,
                status=AgentRunStatus.SUCCEEDED,
                provider="openai",
                model_alias="coding",
                model_id="test-model",
                request={"authorization": f"Bearer {secret}"},
                response={"content": secret},
                total_tokens=20,
                started_at=started,
                completed_at=started + timedelta(seconds=5),
            )
            session.add(agent_run)
            await session.flush()
            command = DevelopmentExecution(
                company_id=company_id,
                project_id=project_id,
                task_id=failed_id,
                task_run_id=run.id,
                agent_run_id=agent_run.id,
                agent_id=agent_id,
                action=DevelopmentAction.GIT_STATUS,
                status=DevelopmentExecutionStatus.FAILED,
                working_directory=f"{company_id}/{project_id}",
                safe_arguments={},
                exit_code=128,
                stdout_excerpt=f"API_KEY={secret}",
                stderr_excerpt="fatal: not a git repository",
                stdout_bytes=len(secret) + 8,
                stderr_bytes=27,
                duration_ms=Decimal("12.5"),
                timeout_seconds=30,
                error_code="DEVELOPMENT_COMMAND_FAILED",
                error_message="Development action exited non-zero.",
                started_at=started + timedelta(seconds=6),
                finished_at=started + timedelta(seconds=7),
                correlation_id=failed_id,
            )
            session.add(command)
            tool = ToolCall(
                agent_run_id=agent_run.id,
                task_run_id=run.id,
                task_id=failed_id,
                agent_id=agent_id,
                tool_name="development.execute",
                status=ToolCallStatus.FAILED,
                arguments={"action": "NODE_TEST", "token": secret, "content": secret},
                error={
                    "code": "DEVELOPMENT_PROFILE_MISMATCH",
                    "message": "NODE_TEST is unavailable for UNKNOWN projects",
                },
                permission="development.execute",
                started_at=started + timedelta(seconds=2),
                completed_at=started + timedelta(seconds=3),
            )
            session.add(tool)
            qa = QAResult(
                task_id=failed_id,
                task_run_id=run.id,
                verifier_agent_id=agent_id,
                iteration=iteration,
                decision=QADecision.FAIL,
                summary="QA found 3 blocking issue(s).",
                checks=[
                    {
                        "name": "GIT_STATUS",
                        "status": "FAILED",
                        "evidence": "GIT_STATUS exited 128.",
                        "execution_id": str(command.id),
                    }
                ],
                blocking_issues=[
                    {"title": "GIT_STATUS failed", "description": "Exit 128."}
                ],
                non_blocking_issues=[],
                correlation_id=failed_id,
            )
            session.add(qa)
            for criterion_index, criterion in enumerate(failed.acceptance_criteria):
                session.add(
                    AcceptanceVerification(
                        task_id=failed_id,
                        task_run_id=run.id,
                        criterion_index=criterion_index,
                        criterion=str(criterion),
                        iteration=iteration,
                        status=AcceptanceVerificationStatus.UNVERIFIED,
                        verifier="FORGE_DETERMINISTIC_QA",
                        verifier_agent_id=agent_id,
                        evidence_summary="No deterministic evidence mapping was available.",
                        execution_ids=[],
                    )
                )
        session.add(
            TaskReview(
                task_id=failed_id,
                iteration=1,
                decision=TaskReviewDecision.FIX_REQUESTED,
                feedback="Initialize the repository and add deterministic tests.",
                correlation_id=failed_id,
            )
        )
        await session.commit()

    response = await client.get(f"/api/v1/tasks/{failed_id}/inspection")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "FAILED"
    assert payload["assigned_agent"]["role"] == "DEVELOPER"
    assert len(payload["iterations"]) == 4
    assert [item["iteration"] for item in payload["iterations"]] == [1, 2, 3, 4]
    final = payload["iterations"][-1]
    assert final["task_run"]["status"] == "FAILED"
    assert final["qa_result"]["decision"] == "FAIL"
    assert final["qa_result"]["deterministic_checks_executed"] is True
    assert final["commands"][0]["exit_code"] == 128
    assert final["commands"][0]["error"]["code"] == "DEVELOPMENT_COMMAND_FAILED"
    assert final["tool_calls"][0]["error"]["code"] == "DEVELOPMENT_PROFILE_MISMATCH"
    assert final["tool_calls"][0]["safe_arguments"]["token"] == "[REDACTED]"
    assert "content omitted" in final["tool_calls"][0]["safe_arguments"]["content"]
    assert payload["failure"]["category"] == "ITERATION_EXHAUSTION"
    assert payload["failure"]["error_code"] == "DEVELOPMENT_ITERATIONS_EXHAUSTED"
    assert payload["failure"]["iteration_exhausted"] is True
    assert payload["failure"]["failing_command"] == "GIT_STATUS"
    assert len(payload["failure"]["failed_acceptance_criteria"]) == 2
    assert payload["dependents"][0]["id"] == str(blocked_id)
    assert payload["dependents"][0]["blocked_reason"] == "BLOCKED_BY_FAILED_DEPENDENCY"
    serialized = response.text
    assert secret not in serialized
    assert "request" not in final["agent_runs"][0]
    assert "response" not in final["agent_runs"][0]

    blocked = await client.get(f"/api/v1/tasks/{blocked_id}/inspection")
    assert blocked.status_code == 200
    assert blocked.json()["dependencies"] == [
        {
            "id": str(failed_id),
            "title": "Implement vertical slice",
            "status": "FAILED",
            "blocked_reason": None,
        }
    ]


async def test_failed_task_inspection_uses_durable_task_run_error_before_fallback(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug=f"failure-source-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    developer = await create_agent(client, company["id"], role="DEVELOPER")
    task_response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": developer["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Inspect a runtime guard failure",
            "acceptance_criteria": ["Landing page exists"],
            "max_iterations": 4,
        },
    )
    task_id = UUID(task_response.json()["id"])
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.FAILED
        task.iteration = 1
        task.started_at = now - timedelta(seconds=5)
        task.completed_at = now
        session.add(
            TaskRun(
                task_id=task.id,
                agent_id=UUID(developer["id"]),
                iteration=1,
                status=TaskRunStatus.FAILED,
                input={},
                error={
                    "code": "DUPLICATE_TOOL_LOOP",
                    "message": "Agent repeated the same unchanged tool action.",
                },
                started_at=now - timedelta(seconds=5),
                completed_at=now,
            )
        )
        await session.commit()

    response = await client.get(f"/api/v1/tasks/{task_id}/inspection")
    assert response.status_code == 200
    failure = response.json()["failure"]
    assert failure["error_code"] == "DUPLICATE_TOOL_LOOP"
    assert failure["category"] == "AGENT_RUNTIME_GUARD"
    assert failure["failing_phase"] == "EXECUTING"
    assert failure["evidence_source"] == "task_run.error"
