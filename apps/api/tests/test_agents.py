from httpx import AsyncClient

from tests.helpers import create_agent, create_company


async def test_agent_create_retrieve_and_company_relationship(client: AsyncClient) -> None:
    company = await create_company(client)
    agent = await create_agent(client, company["id"])

    retrieved = await client.get(f"/api/v1/agents/{agent['id']}")
    listing = await client.get(f"/api/v1/companies/{company['id']}/agents")

    assert retrieved.status_code == 200
    assert retrieved.json()["company_id"] == company["id"]
    assert retrieved.json()["configuration"] == {"model": "none"}
    assert [item["id"] for item in listing.json()] == [agent["id"]]
