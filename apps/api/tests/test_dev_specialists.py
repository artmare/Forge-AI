from uuid import UUID

from sqlalchemy import select

from app.agent_runtime.contracts import ModelResponse
from app.agent_runtime.providers import MockModelProvider
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.domain.models import Event, ToolCall
from app.infrastructure.database import get_session_factory
from tests.helpers import create_agent, create_company, create_project, create_task, transition_task
from tests.test_dev_integration import final, tool


async def test_scoped_advice_limit_and_lead_authority(client, tmp_path, monkeypatch):
    requests = []

    class AdvisoryProvider:
        name, paid = "mock", False

        async def generate(self, request):
            requests.append(request)
            return ModelResponse(
                output={
                    "findings": ["Consider a test"],
                    "risks": [],
                    "recommendations": [],
                    "blocking_issues": [],
                    "optional_suggestions": [],
                }
            )

    monkeypatch.setattr(
        "app.agent_runtime.specialists.provider_for_profile", lambda *args: AdvisoryProvider()
    )
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client, company["id"], role="LEAD_ENGINEER", permissions={"specialist.advise": True}
    )
    task = await create_task(
        client, company["id"], project["id"], agent["id"], title="PRIVATE_FULL_TASK_CONTEXT"
    )
    await transition_task(client, task["id"], "QUEUED")
    provider = MockModelProvider(
        responses=[
            tool(
                "specialist.advise",
                role="ARCHITECT",
                question="Review this narrow boundary",
                context="Only relevant contract",
                constraints=[],
            ),
            tool(
                "specialist.advise",
                role="CODE_REVIEWER",
                question="Review another narrow issue",
                context="Only relevant diff",
                constraints=[],
            ),
            final("Advice received"),
        ]
    )
    settings = Settings(
        forge_dev_mode_enabled=True, forge_dev_specialist_limit=1, tool_workspace_root=str(tmp_path)
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            UUID(task["id"])
        )
        calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == UUID(task["id"]))
                .order_by(ToolCall.created_at)
            )
        )
        assert calls[0].status.value == "SUCCEEDED"
        assert calls[1].error["code"] == "SPECIALIST_CALL_LIMIT"
        assert run.status.value == "SUCCEEDED"
        events = list(
            await session.scalars(
                select(Event).where(
                    Event.task_id == UUID(task["id"]), Event.type == "DEV_SPECIALIST_RESERVED"
                )
            )
        )
        assert len(events) == 1
    assert len(requests) == 1 and requests[0].tools == ()
    assert "Only relevant contract" in requests[0].user_prompt
    assert "PRIVATE_FULL_TASK_CONTEXT" not in requests[0].user_prompt


async def test_nested_specialist_request_is_rejected():
    import pytest

    from app.agent_runtime.specialists import SpecialistCoordinator, SpecialistRequest
    from app.tool_system.errors import ToolSystemError

    coordinator = SpecialistCoordinator(None, Settings())
    with pytest.raises(ToolSystemError, match="Nested"):
        await coordinator.advise(
            SpecialistRequest(role="ARCHITECT", question="Review a boundary", context="scoped"),
            None,
            depth=1,
        )
