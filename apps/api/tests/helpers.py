from typing import Any

from httpx import AsyncClient


async def create_company(client: AsyncClient, slug: str = "test-company") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/companies",
        json={"name": "Test Company", "slug": slug, "goal": "Validate Forge"},
    )
    assert response.status_code == 201
    return response.json()


async def create_project(client: AsyncClient, company_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/companies/{company_id}/projects",
        json={"name": "Test Project", "goal": "Deliver a tested project"},
    )
    assert response.status_code == 201
    return response.json()


async def create_agent(
    client: AsyncClient,
    company_id: str,
    role: str = "GENERAL",
    permissions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/companies/{company_id}/agents",
        json={
            "name": "Developer 1",
            "role": role,
            "configuration": {"model": "none"},
            "permissions": permissions or {},
        },
    )
    assert response.status_code == 201
    return response.json()


async def create_task(
    client: AsyncClient,
    company_id: str,
    project_id: str | None,
    agent_id: str | None,
    *,
    max_iterations: int = 5,
    title: str = "Build homepage",
    priority: str = "NORMAL",
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company_id,
            "project_id": project_id,
            "assigned_agent_id": agent_id,
            "type": "IMPLEMENTATION",
            "title": title,
            "acceptance_criteria": ["Responsive", "No console errors"],
            "priority": priority,
            "max_iterations": max_iterations,
        },
    )
    assert response.status_code == 201
    return response.json()


async def transition_task(
    client: AsyncClient, task_id: str, target_status: str, reason: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"target_status": target_status}
    if reason is not None:
        payload["reason"] = reason
    response = await client.post(f"/api/v1/tasks/{task_id}/transition", json=payload)
    assert response.status_code == 200, response.text
    return response.json()
