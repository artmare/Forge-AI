from httpx import AsyncClient

from tests.helpers import (
    create_agent,
    create_company,
    create_project,
    create_task,
    transition_task,
)


async def test_task_create_retrieve_update_and_filter(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task = await create_task(client, company["id"], project["id"], agent["id"])

    retrieved = await client.get(f"/api/v1/tasks/{task['id']}")
    blocked = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "IN_PROGRESS", "iteration": 1, "priority": "HIGH"},
    )
    updated = await client.patch(f"/api/v1/tasks/{task['id']}", json={"priority": "HIGH"})
    await transition_task(client, task["id"], "QUEUED")
    transitioned = await transition_task(client, task["id"], "IN_PROGRESS")
    filtered = await client.get(
        "/api/v1/tasks",
        params={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "status": "IN_PROGRESS",
        },
    )

    assert retrieved.status_code == 200
    assert retrieved.json()["project_id"] == project["id"]
    assert retrieved.json()["assigned_agent_id"] == agent["id"]
    assert blocked.status_code == 422
    assert updated.json()["status"] == "CREATED"
    assert updated.json()["priority"] == "HIGH"
    assert transitioned["status"] == "IN_PROGRESS"
    assert [item["id"] for item in filtered.json()] == [task["id"]]


async def test_task_rejects_cross_company_assignment(client: AsyncClient) -> None:
    first = await create_company(client, "first-company")
    second = await create_company(client, "second-company")
    project = await create_project(client, first["id"])
    other_agent = await create_agent(client, second["id"])

    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": first["id"],
            "project_id": project["id"],
            "assigned_agent_id": other_agent["id"],
            "type": "TEST",
            "title": "Invalid assignment",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_relationship"


async def test_task_rejects_cross_company_project(client: AsyncClient) -> None:
    first = await create_company(client, "first-project-company")
    second = await create_company(client, "second-project-company")
    project = await create_project(client, first["id"])
    agent = await create_agent(client, second["id"])

    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": second["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "TEST",
            "title": "Invalid project",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_relationship"


async def test_parent_task_must_share_project(client: AsyncClient) -> None:
    company = await create_company(client)
    first_project = await create_project(client, company["id"])
    second_project_response = await client.post(
        f"/api/v1/companies/{company['id']}/projects",
        json={"name": "Second Project", "goal": "Test parent validation"},
    )
    second_project = second_project_response.json()
    agent = await create_agent(client, company["id"])
    parent = await create_task(client, company["id"], first_project["id"], agent["id"])

    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": second_project["id"],
            "assigned_agent_id": agent["id"],
            "parent_task_id": parent["id"],
            "type": "TEST",
            "title": "Invalid parent project",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_relationship"


async def test_task_cannot_be_its_own_parent(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task = await create_task(client, company["id"], project["id"], agent["id"])

    response = await client.patch(
        f"/api/v1/tasks/{task['id']}", json={"parent_task_id": task["id"]}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_relationship"
