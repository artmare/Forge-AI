from httpx import AsyncClient


async def test_system_endpoint_returns_real_counts(client: AsyncClient) -> None:
    company = (
        await client.post(
            "/api/v1/companies",
            json={"name": "Count Co", "slug": "count-co", "goal": "Test counters"},
        )
    ).json()
    agent = (
        await client.post(
            f"/api/v1/companies/{company['id']}/agents",
            json={"name": "Counter", "role": "QA", "configuration": {}, "permissions": {}},
        )
    ).json()
    await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "assigned_agent_id": agent["id"],
            "type": "TEST",
            "title": "Count this task",
        },
    )

    response = await client.get("/api/v1/system")

    assert response.status_code == 200
    assert response.json() == {
        "name": "Forge",
        "version": "0.9.0",
        "status": "healthy",
        "companies": 1,
        "agents": 1,
        "tasks": 1,
        "task_states": {
            "CREATED": 1,
            "QUEUED": 0,
            "IN_PROGRESS": 0,
            "REVIEW": 0,
            "FIX_REQUIRED": 0,
            "DONE": 0,
            "FAILED": 0,
            "CANCELLED": 0,
        },
    }
