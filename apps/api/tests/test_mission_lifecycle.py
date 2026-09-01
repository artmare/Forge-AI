import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.domain.enums import MissionStatus, TaskStatus
from app.domain.models import (
    Agent,
    Company,
    Event,
    Mission,
    MissionDeletionRecord,
    PlanningRun,
    Project,
    Task,
    TaskReview,
)
from app.infrastructure.database import get_session_factory
from app.services.mission_lifecycle import MissionLifecycleService
from app.services.task_state_machine import TaskStateMachine
from app.tool_system.workspace import WorkspaceManager
from tests.test_phase08 import create_mission


async def _count(model) -> int:  # type: ignore[no-untyped-def]
    async with get_session_factory()() as session:
        return int(await session.scalar(select(func.count(model.id))) or 0)


async def _set_status(mission_id: str, status: MissionStatus) -> None:
    async with get_session_factory()() as session:
        mission = await session.get(Mission, UUID(mission_id))
        assert mission is not None
        mission.status = status
        await session.commit()


async def _materialize(client: AsyncClient, title: str, company_id: str | None = None) -> dict:
    mission = await create_mission(client, title)
    if company_id is not None:
        async with get_session_factory()() as session:
            stored = await session.get(Mission, UUID(mission["id"]))
            assert stored is not None
            stored.company_id = UUID(company_id)
            await session.commit()
    planned = await client.post(f"/api/v1/missions/{mission['id']}/plan")
    assert planned.status_code == 200, planned.text
    activated = await client.post(f"/api/v1/missions/{mission['id']}/activate")
    assert activated.status_code == 200, activated.text
    return activated.json()


async def test_archive_completed_and_failed_missions(client: AsyncClient) -> None:
    completed = await create_mission(client, "Completed archive")
    failed = await create_mission(client, "Failed archive")
    await _set_status(completed["id"], MissionStatus.COMPLETED)
    await _set_status(failed["id"], MissionStatus.FAILED)

    for mission in (completed, failed):
        response = await client.post(f"/api/v1/missions/{mission['id']}/archive")
        assert response.status_code == 200, response.text
        assert response.json()["archived_at"] is not None


async def test_archive_active_running_mission_is_rejected(client: AsyncClient) -> None:
    mission = await _materialize(client, "Active archive blocked")
    response = await client.post(f"/api/v1/missions/{mission['id']}/archive")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MISSION_LIFECYCLE_BLOCKED"


async def test_archive_filters_restore_and_preserve_status(client: AsyncClient) -> None:
    visible = await create_mission(client, "Visible Mission")
    archived = await create_mission(client, "Archived Mission")
    await _set_status(archived["id"], MissionStatus.FAILED)
    first = await client.post(f"/api/v1/missions/{archived['id']}/archive")
    second = await client.post(f"/api/v1/missions/{archived['id']}/archive")
    assert first.status_code == second.status_code == 200

    current = (await client.get("/api/v1/missions")).json()
    archive = (await client.get("/api/v1/missions?archive=archived")).json()
    all_missions = (await client.get("/api/v1/missions?archive=all")).json()
    assert {item["id"] for item in current} == {visible["id"]}
    assert {item["id"] for item in archive} == {archived["id"]}
    assert {item["id"] for item in all_missions} == {visible["id"], archived["id"]}

    restored = await client.post(f"/api/v1/missions/{archived['id']}/restore")
    repeated = await client.post(f"/api/v1/missions/{archived['id']}/restore")
    assert restored.status_code == repeated.status_code == 200
    assert restored.json()["archived_at"] is None
    assert restored.json()["status"] == MissionStatus.FAILED.value


async def test_delete_requires_archive_confirmation_and_rejects_active(
    client: AsyncClient,
) -> None:
    mission = await create_mission(client, "Deletion confirmation")
    response = await client.request(
        "DELETE",
        f"/api/v1/missions/{mission['id']}",
        json={"confirmation": mission["title"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MISSION_DELETE_REQUIRES_ARCHIVE"

    active = await _materialize(client, "Inconsistent archived active")
    async with get_session_factory()() as session:
        stored = await session.get(Mission, UUID(active["id"]))
        assert stored is not None
        stored.archived_at = datetime.now(UTC)
        await session.commit()
    blocked = await client.request(
        "DELETE",
        f"/api/v1/missions/{active['id']}",
        json={"confirmation": "DELETE"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MISSION_LIFECYCLE_BLOCKED"


async def test_delete_shared_company_preserves_company_and_removes_project_history(
    client: AsyncClient,
) -> None:
    company = (
        await client.post(
            "/api/v1/companies",
            json={"name": "Shared Company", "slug": "shared-lifecycle", "goal": "Shared"},
        )
    ).json()
    mission = await _materialize(client, "Shared company Mission", company["id"])
    graph = (await client.get(f"/api/v1/projects/{mission['project_id']}/graph")).json()
    root = next(node for node in graph["nodes"] if node["status"] == TaskStatus.QUEUED)
    async with get_session_factory()() as session:
        machine = TaskStateMachine(session)
        await machine.transition(UUID(root["id"]), TaskStatus.IN_PROGRESS)
        await machine.transition(UUID(root["id"]), TaskStatus.REVIEW)
    assert (await client.post(f"/api/v1/tasks/{root['id']}/approve")).status_code == 200
    assert await _count(TaskReview) == 1
    assert (await client.post(f"/api/v1/missions/{mission['id']}/cancel")).status_code == 200
    assert (await client.post(f"/api/v1/missions/{mission['id']}/archive")).status_code == 200

    deleted = await client.request(
        "DELETE",
        f"/api/v1/missions/{mission['id']}",
        json={"confirmation": mission["title"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["company_deleted"] is False
    assert deleted.json()["project_deleted"] is True
    async with get_session_factory()() as session:
        assert await session.get(Company, UUID(company["id"])) is not None
        assert await session.get(Project, UUID(mission["project_id"])) is None
        assert await session.get(Mission, UUID(mission["id"])) is None
    assert await _count(Task) == 0
    assert await _count(TaskReview) == 0
    assert await _count(Agent) == 0
    assert await _count(PlanningRun) == 0
    assert await _count(MissionDeletionRecord) == 1
    event_types = {event.type for event in await _events()}
    assert {"MISSION_DELETION_REQUESTED", "MISSION_DELETED"} <= event_types


async def _events() -> list[Event]:
    async with get_session_factory()() as session:
        return list(await session.scalars(select(Event)))


async def test_owned_company_and_workspace_are_deleted_safely(
    client: AsyncClient, tmp_path: Path
) -> None:
    mission = await _materialize(client, "Owned disposable Mission")
    assert mission["owns_company"] is True
    assert mission["owns_project"] is True
    assert (await client.post(f"/api/v1/missions/{mission['id']}/cancel")).status_code == 200
    assert (await client.post(f"/api/v1/missions/{mission['id']}/archive")).status_code == 200
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.project_workspace(
        UUID(mission["company_id"]), UUID(mission["project_id"])
    )
    (workspace / "artifact.txt").write_text("disposable", encoding="utf-8")

    async with get_session_factory()() as session:
        result = await MissionLifecycleService(
            session, Settings(tool_workspace_root=str(tmp_path / "workspaces"))
        ).delete(UUID(mission["id"]), mission["title"])
    assert result.company_deleted is True
    assert result.project_deleted is True
    assert result.workspace_cleanup_status == "COMPLETED"
    assert not workspace.exists()
    async with get_session_factory()() as session:
        assert await session.get(Company, UUID(mission["company_id"])) is None
        record = await session.scalar(
            select(MissionDeletionRecord).where(
                MissionDeletionRecord.mission_id == UUID(mission["id"])
            )
        )
        assert record is not None and record.workspace_cleanup_status == "COMPLETED"


async def test_concurrent_archive_is_idempotent(client: AsyncClient) -> None:
    mission = await create_mission(client, "Concurrent archive")
    await _set_status(mission["id"], MissionStatus.COMPLETED)
    responses = await asyncio.gather(
        client.post(f"/api/v1/missions/{mission['id']}/archive"),
        client.post(f"/api/v1/missions/{mission['id']}/archive"),
    )
    assert [response.status_code for response in responses] == [200, 200]
    events = [event for event in await _events() if event.type == "MISSION_ARCHIVED"]
    assert len(events) == 1


async def test_concurrent_delete_allows_one_destructive_winner(client: AsyncClient) -> None:
    mission = await create_mission(client, "Concurrent delete")
    assert (await client.post(f"/api/v1/missions/{mission['id']}/archive")).status_code == 200

    async def remove():  # type: ignore[no-untyped-def]
        return await client.request(
            "DELETE",
            f"/api/v1/missions/{mission['id']}",
            json={"confirmation": mission["title"]},
        )

    responses = await asyncio.gather(remove(), remove())
    assert sorted(response.status_code for response in responses) == [200, 404]
    assert await _count(MissionDeletionRecord) == 1
