from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.agent_runtime.contracts import ModelResponse, ModelToolCall, ProviderCallError
from app.agent_runtime.openrouter_catalog import OpenRouterCatalogClient
from app.agent_runtime.probes import CapabilityProber, ProbeCapability

FINAL = {
    "turn": {
        "type": "final",
        "result": {
            "status": "completed",
            "summary": "FORGE_PROBE_OK",
            "output": {"artifacts": [], "details": [], "execution_claims": []},
        },
    }
}


class Provider:
    name, paid = "fake", False

    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def generate(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def native(arguments=None):
    return ModelResponse(
        output={},
        tool_call=ModelToolCall(
            "forge_probe.echo", arguments or {"value": "FORGE_PROBE_OK"}, call_id="probe-1"
        ),
    )


@pytest.mark.parametrize(
    "capability,responses,status",
    [
        (ProbeCapability.TOOL_CALLING, [native()], "verified"),
        (ProbeCapability.TOOL_CALLING, [ModelResponse(output="I called it")], "protocol_failure"),
        (ProbeCapability.TOOL_CALLING, [native({"value": "wrong"})], "protocol_failure"),
        (ProbeCapability.TOOL_CONTINUATION, [native(), ModelResponse(output=FINAL)], "verified"),
        (ProbeCapability.TOOL_CONTINUATION, [native(), native()], "protocol_failure"),
        (ProbeCapability.STRUCTURED_OUTPUT, [ModelResponse(output=FINAL)], "verified"),
        (
            ProbeCapability.STRUCTURED_OUTPUT,
            [ModelResponse(output="completed")],
            "protocol_failure",
        ),
        (
            ProbeCapability.TOOL_CALLING,
            [ProviderCallError("MODEL_RATE_LIMIT", "limited", http_status=429)],
            "rate_limited",
        ),
        (ProbeCapability.TOOL_CALLING, [TimeoutError()], "temporarily_unavailable"),
    ],
)
async def test_behavioral_probe(capability, responses, status):
    provider = Provider(responses)
    result = await CapabilityProber().probe(provider, "model", capability)
    assert result.status == status
    if capability == ProbeCapability.TOOL_CONTINUATION and status == "verified":
        assert provider.requests[1].tool_exchanges[0].response["status"] == "success"


async def test_probe_cache_and_retry_expiry():
    provider = Provider([native(), native()])
    prober = CapabilityProber(ttl_seconds=60)
    a = await prober.probe(provider, "model", ProbeCapability.TOOL_CALLING)
    assert await prober.probe(provider, "model", ProbeCapability.TOOL_CALLING) is a
    assert len(provider.requests) == 1
    await prober.probe(provider, "model", ProbeCapability.TOOL_CALLING, force=True)
    assert len(provider.requests) == 2


async def test_catalog_failed_refresh_retains_last_good_and_backs_off():
    responses = [
        httpx.Response(
            200, json={"data": [{"id": "a:free", "pricing": {"prompt": "0", "completion": "0"}}]}
        ),
        httpx.Response(503),
    ]

    def handler(request):
        return responses.pop(0)

    client = httpx.AsyncClient(
        base_url="https://example.invalid/", transport=httpx.MockTransport(handler)
    )
    catalog = OpenRouterCatalogClient(
        base_url="https://example.invalid", api_key=None, client=client
    )
    first = await catalog.refresh()
    catalog._cached_at = datetime.now(UTC) - timedelta(hours=2)
    assert await catalog.refresh() == first
    assert catalog.status["last_error"] == "MODEL_PROVIDER_TEMPORARY"
    assert await catalog.refresh() == first
    assert not responses
    await client.aclose()


@pytest.mark.parametrize(
    "pricing,expected",
    [
        ({"prompt": "1", "completion": "0"}, False),
        ({"prompt": "0", "completion": "0"}, True),
        ({"prompt": "invalid", "completion": "0"}, False),
        ({}, True),
    ],
)
def test_authoritative_pricing_beats_free_suffix(pricing, expected):
    assert OpenRouterCatalogClient._parse({"id": "a:free", "pricing": pricing}).free is expected
