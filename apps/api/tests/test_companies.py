from uuid import uuid4

from httpx import AsyncClient

from tests.helpers import create_company


async def test_company_create_list_retrieve_and_update(client: AsyncClient) -> None:
    company = await create_company(client)

    listing = await client.get("/api/v1/companies")
    retrieved = await client.get(f"/api/v1/companies/{company['id']}")
    updated = await client.patch(
        f"/api/v1/companies/{company['id']}",
        json={"name": "Updated Company", "status": "ACTIVE"},
    )

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [company["id"]]
    assert retrieved.json()["slug"] == "test-company"
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Company"
    assert updated.json()["status"] == "ACTIVE"


async def test_company_invalid_id_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/companies/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_duplicate_company_slug_returns_409(client: AsyncClient) -> None:
    await create_company(client, "unique-company")
    response = await client.post(
        "/api/v1/companies",
        json={"name": "Duplicate", "slug": "unique-company", "goal": "Should conflict"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_company_slug"


async def test_company_update_rejects_null_required_field(client: AsyncClient) -> None:
    company = await create_company(client, "null-update")

    response = await client.patch(f"/api/v1/companies/{company['id']}", json={"name": None})

    assert response.status_code == 422
