from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.domain.models import ProjectKnowledgeIndex
from app.infrastructure.database import get_session_factory
from app.services.project_knowledge import ProjectKnowledgeService
from tests.helpers import create_company, create_project


async def test_project_brain_persists_checkpoint_and_lesson(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    project_id = UUID(project["id"])
    task_id = UUID("11111111-1111-1111-1111-111111111111")

    async with get_session_factory()() as session:
        service = ProjectKnowledgeService(session)
        await service.checkpoint(
            project_id,
            task_id=task_id,
            goal="Implement execution truth",
            completed=["Added validator"],
            current_diff="diff --git a/runtime.py b/runtime.py",
            decisions=["Use ToolCall evidence"],
            test_status=["unit tests passed"],
            failures=[],
            open_questions=[],
            next_action="Run integration suite",
        )
        await service.record_lesson(
            project_id,
            task_id=task_id,
            lesson="Model prose is not execution evidence",
            evidence="No ToolCall existed for the claimed write",
        )
        await session.commit()

    async with get_session_factory()() as session:
        brain = await session.scalar(
            select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == project_id)
        )

    assert brain is not None
    assert brain.checkpoints[0]["next_action"] == "Run integration suite"
    assert brain.lessons[0]["lesson"] == "Model prose is not execution evidence"
