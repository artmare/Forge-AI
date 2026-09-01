from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.api.routes import health as health_route


def async_result(value: bool) -> Callable[[], Awaitable[bool]]:
    async def result() -> bool:
        return value

    return result


async def test_health_endpoint_reports_live_dependencies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_route, "check_database", async_result(True))
    monkeypatch.setattr(health_route, "check_redis", async_result(True))

    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "services": {
            "api": "healthy",
            "database": "healthy",
            "redis": "healthy",
        },
    }


async def test_health_endpoint_returns_503_for_dependency_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_route, "check_database", async_result(False))
    monkeypatch.setattr(health_route, "check_redis", async_result(True))

    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["services"]["database"] == "unhealthy"
