import httpx

from app.agent_runtime.openrouter_catalog import OpenRouterCatalogClient
from app.agent_runtime.routing import EconomicTier, ModelCapability


async def test_openrouter_catalog_discovers_free_tool_capable_model_and_caches() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/free-coder",
                        "name": "Free Coder",
                        "context_length": 32768,
                        "pricing": {"prompt": "0", "completion": "0.000000"},
                        "supported_parameters": ["tools", "tool_choice", "response_format"],
                        "forge_capabilities": ["CODING", "REASONING"],
                        "architecture": {"input_modalities": ["text"]},
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test/v1/")
    catalog = OpenRouterCatalogClient(base_url="https://test/v1", api_key=None, client=http)

    first = await catalog.discover()
    second = await catalog.discover()

    assert first == second
    assert calls == 1
    assert first[0].free
    assert ModelCapability.TOOL_CALLING in first[0].capabilities
    assert ModelCapability.STRUCTURED_OUTPUT in first[0].capabilities
    assert first[0].profile().tier == EconomicTier.FREE


async def test_openrouter_catalog_does_not_invent_coding_capability() -> None:
    entry = OpenRouterCatalogClient._parse(
        {
            "id": "vendor/unknown",
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["tools"],
        }
    )

    assert entry is not None
    assert ModelCapability.CODING not in entry.capabilities
