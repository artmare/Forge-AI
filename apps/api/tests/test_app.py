from httpx import AsyncClient


async def test_application_starts(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Forge API"


async def test_unknown_route_uses_consistent_error(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "http_404", "message": "Not Found"}}


async def test_invalid_payload_uses_consistent_error(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/companies", json={"name": "", "slug": "Not Valid", "goal": ""}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "The request data is invalid."}
    }
