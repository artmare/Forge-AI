"""Exercise the shipped sample through normal routing, never an injected provider."""

import os

import pytest


@pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="requires the isolated Compose development runners",
)
async def test_explicit_mock_todo_uses_normal_development_qa_and_review(client):
    created = await client.post("/api/v1/missions", json={
        "title": "Mock development regression",
        "goal": "Run the explicit Todo sample through the real development pipeline.",
        "context": {"mock_scenario": "todo_demo"},
    })
    assert created.status_code == 201, created.text
    mission = created.json()
    planned = await client.post(f"/api/v1/missions/{mission['id']}/plan")
    assert planned.status_code == 200, planned.text
    assert planned.json()["provider"] == "mock"
    assert planned.json()["status"] == "SUCCEEDED"
    activated = await client.post(f"/api/v1/missions/{mission['id']}/activate")
    assert activated.status_code == 200, activated.text
    project_id = activated.json()["project_id"]
    graph = (await client.get(f"/api/v1/projects/{project_id}/graph")).json()
    task = graph["nodes"][0]
    assert task["kind"] == "DEVELOPMENT"
    executed = await client.post(f"/api/v1/tasks/{task['id']}/execute", json={})
    assert executed.status_code == 200, executed.text
    graph = (await client.get(f"/api/v1/projects/{project_id}/graph")).json()
    task = graph["nodes"][0]
    assert task["status"] == "REVIEW", task
    assert task["qa_results"][-1]["decision"] == "PASS"
    assert all(v["status"] == "PASSED" for v in task["acceptance_verifications"])
    actions = {e["action"] for e in task["development_executions"] if e["status"] == "SUCCEEDED"}
    assert {"NODE_TEST", "NODE_BUILD", "GIT_STATUS", "GIT_DIFF"} <= actions
    profile = (await client.get(f"/api/v1/projects/{project_id}/development-profile")).json()
    assert profile["repository_initialized"] is True
    assert profile["initial_checkpoint_created"] is True
    assert (await client.post(f"/api/v1/tasks/{task['id']}/approve")).status_code == 200
    finished = (await client.get(f"/api/v1/missions/{mission['id']}")).json()
    assert finished["status"] == "COMPLETED"
