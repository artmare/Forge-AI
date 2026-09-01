from httpx import AsyncClient

from tests.helpers import create_company, create_project


async def test_project_create_retrieve_and_company_relationship(client: AsyncClient) -> None:
    company = await create_company(client)
    project = await create_project(client, company["id"])

    retrieved = await client.get(f"/api/v1/projects/{project['id']}")
    listing = await client.get(f"/api/v1/companies/{company['id']}/projects")

    assert retrieved.status_code == 200
    assert retrieved.json()["company_id"] == company["id"]
    assert [item["id"] for item in listing.json()] == [project["id"]]
