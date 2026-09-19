from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from httpx import AsyncClient

from app.agent_runtime.contracts import (
    BaseAgentResult,
    ModelRequest,
    ModelResponse,
    ModelTool,
    ModelToolCall,
    ModelToolExchange,
    ProviderCallError,
)
from app.agent_runtime.economics import ModelEconomicsService
from app.agent_runtime.policy import ModelExecutionPolicy
from app.agent_runtime.providers import GeminiModelProvider
from app.agent_runtime.registry import ModelRegistry
from app.agent_runtime.routing import (
    EconomicTier,
    ModelCapability,
    ModelProfile,
    ModelRouter,
    ProviderHealthStatus,
    RoutingContext,
    required_capabilities,
)
from app.core.config import Settings
from app.domain.exceptions import AgentRuntimeDomainError, PaidModelCallDisabledError
from app.domain.models import Mission, ModelProviderHealth, Task
from app.infrastructure.database import get_session_factory
from app.planning.contracts import PlanProposal
from app.tool_system.contracts import AgentTurnResponse
from app.tool_system.filesystem import FilesystemPathInput
from tests.helpers import create_agent, create_company, create_project, create_task

ALL = frozenset(ModelCapability)


def profile(
    alias: str,
    *,
    provider: str = "mock",
    tier: EconomicTier = EconomicTier.FREE,
    capabilities: frozenset[ModelCapability] = ALL,
    enabled: bool = True,
    paid: bool = False,
    reserve: float | None = 0,
) -> ModelProfile:
    return ModelProfile(
        alias=alias,
        provider=provider,
        model_id=f"{provider}/{alias}",
        tier=tier,
        capabilities=capabilities,
        enabled=enabled,
        paid=paid,
        max_call_cost=reserve,
    )


def context(**changes: object) -> RoutingContext:
    values: dict[str, object] = {
        "required_capabilities": frozenset({ModelCapability.TEXT}),
        "paid_budget_remaining": 0,
    }
    values.update(changes)
    return RoutingContext(**values)  # type: ignore[arg-type]


def test_free_model_is_selected_first() -> None:
    selected = ModelRouter([profile("free")]).candidates(context())
    assert selected[0].profile.alias == "free"
    assert selected[0].profile.tier == EconomicTier.FREE


def test_requested_free_alias_wins_tie() -> None:
    selected = ModelRouter([profile("alpha"), profile("beta")]).candidates(
        context(requested_alias="beta")
    )
    assert selected[0].profile.alias == "beta"


def test_capability_filter_excludes_incompatible_model() -> None:
    selected = ModelRouter(
        [
            profile("text", capabilities=frozenset({ModelCapability.TEXT})),
            profile(
                "coder",
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODING}),
            ),
        ]
    ).candidates(
        context(required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODING}))
    )
    assert [item.profile.alias for item in selected] == ["coder"]


def test_missing_capability_fails_closed() -> None:
    with pytest.raises(ProviderCallError, match="required capabilities") as raised:
        ModelRouter([profile("text", capabilities=frozenset({ModelCapability.TEXT}))]).candidates(
            context(required_capabilities=frozenset({ModelCapability.VISION}))
        )
    assert raised.value.code == "MODEL_CAPABILITY_UNAVAILABLE"


def test_disabled_profile_is_not_routable() -> None:
    with pytest.raises(ProviderCallError) as raised:
        ModelRouter([profile("off", enabled=False)]).candidates(context())
    assert raised.value.code == "MODEL_CAPABILITY_UNAVAILABLE"


def test_quota_exhausted_free_provider_fails_over_to_another_free_provider() -> None:
    first = profile("first", provider="gemini")
    second = profile("second", provider="openrouter")
    selected = ModelRouter([first, second]).candidates(
        context(
            provider_health={(first.provider, first.model_id): ProviderHealthStatus.QUOTA_EXHAUSTED}
        )
    )
    assert [item.profile.alias for item in selected] == ["second"]


def test_auth_failed_provider_is_excluded() -> None:
    target = profile("free", provider="gemini")
    with pytest.raises(ProviderCallError) as raised:
        ModelRouter([target]).candidates(
            context(
                provider_health={
                    (target.provider, target.model_id): ProviderHealthStatus.AUTH_FAILED
                }
            )
        )
    assert raised.value.code == "MODEL_PROVIDER_UNAVAILABLE"


def test_protocol_failure_model_is_skipped_for_healthy_fallback() -> None:
    unreliable = profile("unreliable", provider="openrouter")
    fallback = profile("fallback", provider="openrouter")
    selected = ModelRouter([unreliable, fallback]).candidates(
        context(
            provider_health={
                (unreliable.provider, unreliable.model_id): ProviderHealthStatus.PROTOCOL_FAILURE
            }
        )
    )

    assert [item.profile.alias for item in selected] == ["fallback"]


async def test_transient_provider_health_recovers_after_bounded_cooldown() -> None:
    target = profile("free", provider="gemini")
    settings = Settings(
        gemini_enabled=True,
        gemini_api_key="test-key",
        model_provider_health_cooldown_seconds=300,
    )
    async with get_session_factory()() as session:
        session.add(
            ModelProviderHealth(
                provider=target.provider,
                model_id=target.model_id,
                status=ProviderHealthStatus.QUOTA_EXHAUSTED.value,
                failure_code="MODEL_QUOTA_EXHAUSTED",
                last_checked_at=datetime.now(UTC) - timedelta(seconds=301),
            )
        )
        await session.commit()
        health = await ModelEconomicsService(session, settings).provider_health((target,))
    assert health[(target.provider, target.model_id)] == ProviderHealthStatus.HEALTHY


async def test_fresh_quota_exhaustion_and_auth_failure_remain_fail_closed() -> None:
    quota = profile("quota", provider="gemini")
    auth = profile("auth", provider="openrouter")
    settings = Settings(
        gemini_enabled=True,
        gemini_api_key="test-key",
        openrouter_enabled=True,
        openrouter_api_key="test-key",
        model_provider_health_cooldown_seconds=300,
    )
    async with get_session_factory()() as session:
        session.add_all(
            [
                ModelProviderHealth(
                    provider=quota.provider,
                    model_id=quota.model_id,
                    status=ProviderHealthStatus.QUOTA_EXHAUSTED.value,
                    last_checked_at=datetime.now(UTC),
                ),
                ModelProviderHealth(
                    provider=auth.provider,
                    model_id=auth.model_id,
                    status=ProviderHealthStatus.AUTH_FAILED.value,
                    last_checked_at=datetime.now(UTC) - timedelta(days=1),
                ),
            ]
        )
        await session.commit()
        health = await ModelEconomicsService(session, settings).provider_health((quota, auth))
    assert health[(quota.provider, quota.model_id)] == ProviderHealthStatus.QUOTA_EXHAUSTED
    assert health[(auth.provider, auth.model_id)] == ProviderHealthStatus.AUTH_FAILED


def test_paid_candidate_requires_budget() -> None:
    paid = profile(
        "cheap",
        tier=EconomicTier.CHEAP,
        provider="openrouter",
        paid=True,
        reserve=0.01,
    )
    with pytest.raises(ProviderCallError) as raised:
        ModelRouter([paid]).candidates(context(paid_budget_remaining=0))
    assert raised.value.code == "MODEL_BUDGET_UNAVAILABLE"


def test_paid_candidate_reservation_must_fit_budget() -> None:
    paid = profile(
        "cheap",
        tier=EconomicTier.CHEAP,
        provider="openrouter",
        paid=True,
        reserve=0.5,
    )
    with pytest.raises(ProviderCallError) as raised:
        ModelRouter([paid]).candidates(context(paid_budget_remaining=0.1))
    assert raised.value.code == "MODEL_PROVIDER_UNAVAILABLE"


def test_cheap_candidate_is_available_after_free_candidate() -> None:
    selected = ModelRouter(
        [
            profile("free"),
            profile(
                "cheap",
                provider="openrouter",
                tier=EconomicTier.CHEAP,
                paid=True,
                reserve=0.01,
            ),
        ]
    ).candidates(context(paid_budget_remaining=1))
    assert [item.profile.tier for item in selected] == [EconomicTier.FREE, EconomicTier.CHEAP]


def test_cheap_is_selected_when_free_lacks_required_capability() -> None:
    selected = ModelRouter(
        [
            profile("free-text", capabilities=frozenset({ModelCapability.TEXT})),
            profile(
                "cheap-coder",
                provider="openrouter",
                tier=EconomicTier.CHEAP,
                capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODING}),
                paid=True,
                reserve=0.01,
            ),
        ]
    ).candidates(
        context(
            required_capabilities=frozenset(
                {ModelCapability.TEXT, ModelCapability.CODING}
            ),
            paid_budget_remaining=1,
        )
    )
    assert [item.profile.alias for item in selected] == ["cheap-coder"]


def test_premium_requires_explicit_justification() -> None:
    premium = profile(
        "premium",
        provider="openai",
        tier=EconomicTier.PREMIUM,
        paid=True,
        reserve=0.05,
    )
    selected = ModelRouter([profile("free"), premium]).candidates(context(paid_budget_remaining=1))
    assert [item.profile.alias for item in selected] == ["free"]


def test_only_compatible_premium_is_capability_justified() -> None:
    premium = profile(
        "premium",
        provider="openai",
        tier=EconomicTier.PREMIUM,
        paid=True,
        reserve=0.05,
    )
    selected = ModelRouter([premium]).candidates(context(paid_budget_remaining=1))
    assert "CAPABILITY_UNAVAILABLE" in selected[0].reason


def test_premium_provider_failover_is_explainable() -> None:
    premium = profile(
        "premium",
        provider="openai",
        tier=EconomicTier.PREMIUM,
        paid=True,
        reserve=0.05,
    )
    selected = ModelRouter([premium]).candidates(
        context(paid_budget_remaining=1, escalation_reason="PROVIDER_FAILOVER")
    )
    assert "PROVIDER_FAILOVER" in selected[0].reason


def test_registry_parses_provider_neutral_catalog() -> None:
    registry = ModelRegistry.from_settings(
        Settings(
            model_catalog=[
                {
                    "alias": "planner-free",
                    "provider": "gemini",
                    "model": "gemini-free",
                    "tier": "FREE",
                    "capabilities": ["TEXT", "REASONING", "STRUCTURED_OUTPUT"],
                    "paid": False,
                }
            ]
        )
    )
    resolved = registry.resolve("planner-free")
    assert resolved.provider == "gemini"
    assert ModelCapability.STRUCTURED_OUTPUT in resolved.capabilities


def test_role_capabilities_are_deterministic() -> None:
    capabilities = required_capabilities(role="DEVELOPER", has_tools=True, structured_output=True)
    assert capabilities == frozenset(
        {
            ModelCapability.TEXT,
            ModelCapability.CODING,
            ModelCapability.TOOL_CALLING,
            ModelCapability.STRUCTURED_OUTPUT,
        }
    )


def test_empty_optional_spend_limits_are_treated_as_unset() -> None:
    settings = Settings(
        max_estimated_cost_per_task="",  # type: ignore[arg-type]
        max_ai_spend_per_task="",  # type: ignore[arg-type]
        max_ai_spend_per_mission="",  # type: ignore[arg-type]
    )
    assert settings.max_estimated_cost_per_task is None
    assert settings.max_ai_spend_per_task is None
    assert settings.max_ai_spend_per_mission is None


def test_paid_global_gate_remains_fail_closed() -> None:
    class PaidProvider:
        name = "openai"
        paid = True

    with pytest.raises(PaidModelCallDisabledError):
        ModelExecutionPolicy(Settings(allow_paid_model_calls=False)).authorize(PaidProvider())


def test_provider_enablement_and_credentials_are_checked_without_exposing_key() -> None:
    class GeminiProvider:
        name = "gemini"
        paid = False

    settings = Settings(gemini_enabled=True, gemini_api_key=None)
    with pytest.raises(AgentRuntimeDomainError) as raised:
        ModelExecutionPolicy(settings).authorize(GeminiProvider())
    assert raised.value.code == "MODEL_AUTH_ERROR"
    assert "key" not in raised.value.message.lower()


async def test_mock_planning_persists_free_call_and_safe_economics_view(
    client: AsyncClient,
) -> None:
    created = await client.post(
        "/api/v1/missions",
        json={"title": "Economic audit", "goal": "Create a safe deterministic plan."},
    )
    assert created.status_code == 201
    mission_id = created.json()["id"]
    planned = await client.post(f"/api/v1/missions/{mission_id}/plan")
    assert planned.status_code == 200

    response = await client.get(f"/api/v1/economics/missions/{mission_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_calls"] == 1
    assert payload["calls_by_tier"] == {"FREE": 1}
    assert Decimal(payload["ai_spend_recorded"]) == 0
    assert "FREE selected" in payload["recent_calls"][0]["selection_reason"]
    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "authorization" not in serialized


async def test_zero_paid_budget_is_the_mission_default(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/missions",
        json={"title": "Zero capital", "goal": "Remain on free deterministic resources."},
    )
    assert created.status_code == 201
    payload = created.json()
    assert Decimal(payload["paid_ai_budget"]) == 0
    assert Decimal(payload["ai_spend_recorded"]) == 0


async def test_mission_and_task_spend_limits_block_pre_call_reservation(
    client: AsyncClient,
) -> None:
    mission_response = await client.post(
        "/api/v1/missions",
        json={
            "title": "Bounded mission",
            "goal": "Prove mission accounting is fail closed.",
            "paid_ai_budget": "1.00",
        },
    )
    assert mission_response.status_code == 201
    company = await create_company(client, slug="bounded-task-company")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task_payload = await create_task(client, company["id"], project["id"], agent["id"])
    paid = profile(
        "bounded-paid",
        provider="openrouter",
        tier=EconomicTier.CHEAP,
        paid=True,
        reserve=0.02,
    )
    selection = ModelRouter([paid]).candidates(context(paid_budget_remaining=1))[0]

    async with get_session_factory()() as session:
        mission = await session.get(Mission, UUID(mission_response.json()["id"]))
        assert mission is not None
        with pytest.raises(ProviderCallError) as mission_error:
            await ModelEconomicsService(
                session,
                Settings(
                    allow_paid_model_calls=True,
                    max_ai_spend_per_mission=0.01,
                ),
            ).begin_call(
                selection,
                capabilities=frozenset({ModelCapability.TEXT.value}),
                agent_role="PLANNER",
                mission=mission,
            )
        assert mission_error.value.code == "MODEL_BUDGET_UNAVAILABLE"

    async with get_session_factory()() as session:
        task = await session.get(Task, UUID(task_payload["id"]))
        assert task is not None
        task.paid_ai_budget = Decimal("1.00")
        await session.commit()
        with pytest.raises(ProviderCallError) as task_error:
            await ModelEconomicsService(
                session,
                Settings(
                    allow_paid_model_calls=True,
                    max_ai_spend_per_task=0.01,
                ),
            ).begin_call(
                selection,
                capabilities=frozenset({ModelCapability.TEXT.value}),
                agent_role="GENERAL",
                task=task,
            )
        assert task_error.value.code == "MODEL_BUDGET_UNAVAILABLE"


async def test_task_model_call_limit_stops_additional_provider_reservation(
    client: AsyncClient,
) -> None:
    company = await create_company(client, slug="call-limit-company")
    project = await create_project(client, company["id"])
    agent = await create_agent(client, company["id"])
    task_payload = await create_task(client, company["id"], project["id"], agent["id"])
    free = profile("bounded-free")
    selection = ModelRouter([free]).candidates(context())[0]

    async with get_session_factory()() as session:
        task = await session.get(Task, UUID(task_payload["id"]))
        assert task is not None
        economics = ModelEconomicsService(
            session,
            Settings(max_model_calls_per_task=1, max_free_model_calls_per_task=1),
        )
        record = await economics.begin_call(
            selection,
            capabilities=frozenset({ModelCapability.TEXT.value}),
            agent_role="GENERAL",
            task=task,
        )
        await economics.complete_call(record.id, response=ModelResponse(output={}))
        with pytest.raises(ProviderCallError) as raised:
            await economics.begin_call(
                selection,
                capabilities=frozenset({ModelCapability.TEXT.value}),
                agent_role="GENERAL",
                task=task,
            )
        assert raised.value.code == "MODEL_CALL_BUDGET_EXHAUSTED"


async def test_gemini_adapter_validates_structured_output() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-secret"
        assert request.url.path == "/v1beta/models/gemini-test:generateContent"
        payload = __import__("json").loads(request.content)
        status_schema = payload["generationConfig"]["responseJsonSchema"]["properties"][
            "status"
        ]
        assert status_schema["enum"] == ["completed"]
        assert "const" not in status_schema
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"status":"completed","summary":"ok",'
                                    '"output":{"artifacts":[],"details":[]},"notes":[]}'
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
            },
        )

    provider = GeminiModelProvider(
        "test-secret", 1, base_url="https://gemini.invalid/v1beta", paid=False
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid/v1beta/",
        transport=httpx.MockTransport(handler),
        headers={"x-goog-api-key": "test-secret"},
    )
    response = await provider.generate(
        ModelRequest(
            model="gemini-test",
            system_prompt="system",
            user_prompt="user",
            response_model=BaseAgentResult,
        )
    )
    await provider.client.aclose()
    assert isinstance(response.output, BaseAgentResult)
    assert response.usage.total_tokens == 0


def _planner_payload(*, acceptance_criteria: object = None) -> dict:
    criteria = ["The result is verifiable"] if acceptance_criteria is None else acceptance_criteria
    return {
        "project": {"name": "NEXA", "description": "A bounded product plan."},
        "agents": [
            {
                "key": "developer",
                "name": "Developer",
                "role": "DEVELOPER",
                "description": "Implements the project.",
                "model_alias": "developer_coding",
                "requested_permissions": {
                    "filesystem.list": True,
                    "filesystem.read": True,
                    "filesystem.write": True,
                    "development.execute": True,
                    "development.install_dependencies": False,
                    "git.read": True,
                    "git.write": True,
                },
            }
        ],
        "tasks": [
            {
                "key": "implement_nexa",
                "title": "Implement NEXA",
                "description": "Create the bounded deliverable.",
                "assigned_agent_key": "developer",
                "priority": "NORMAL",
                "kind": "DEVELOPMENT",
                "input": {
                    "objective": "Implement NEXA",
                    "deliverables": ["application"],
                    "source_files": [],
                    "constraints": ["No network access"],
                },
                "acceptance_criteria": criteria,
                "max_iterations": 2,
            }
        ],
        "dependencies": [],
    }


async def test_gemini_real_planner_schema_preserves_nested_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        schema = payload["generationConfig"]["responseJsonSchema"]
        task_schema = schema["$defs"]["ProposedTask"]
        key_schema = task_schema["properties"]["key"]
        assert key_schema["type"] == "string"
        assert "minLength" not in key_schema
        assert "maxLength" not in key_schema
        assert "pattern" not in key_schema
        assert "minimum length 1" in key_schema["description"]
        assert "maximum length 80" in key_schema["description"]
        assert "must match pattern" in key_schema["description"]
        assert task_schema["properties"]["acceptance_criteria"]["type"] == "array"
        assert task_schema["properties"]["input"]["additionalProperties"] is False
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": __import__("json").dumps(_planner_payload())}
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    provider = GeminiModelProvider(
        "test-secret", 1, base_url="https://gemini.invalid/v1beta", paid=False
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid/v1beta/",
        transport=httpx.MockTransport(handler),
        headers={"x-goog-api-key": "test-secret"},
    )
    response = await provider.generate(
        ModelRequest(
            model="gemini-test",
            system_prompt="system",
            user_prompt="user",
            response_model=PlanProposal,
        )
    )
    await provider.client.aclose()
    assert isinstance(response.output, PlanProposal)
    assert response.output.tasks[0].acceptance_criteria == ["The result is verifiable"]


async def test_gemini_planner_validation_error_has_safe_exact_field_path() -> None:
    secret_raw_value = "secret-acceptance-value"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": __import__("json").dumps(
                                        _planner_payload(acceptance_criteria=secret_raw_value)
                                    )
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    provider = GeminiModelProvider(
        "test-secret", 1, base_url="https://gemini.invalid/v1beta", paid=False
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid/v1beta/",
        transport=httpx.MockTransport(handler),
        headers={"x-goog-api-key": "test-secret"},
    )
    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(
            ModelRequest(
                model="gemini-test",
                system_prompt="system",
                user_prompt="user",
                response_model=PlanProposal,
            )
        )
    await provider.client.aclose()

    assert raised.value.code == "INVALID_MODEL_OUTPUT"
    assert raised.value.http_status == 200
    assert raised.value.details["finish_reason"] == "STOP"
    assert raised.value.details["content_exists"] is True
    assert raised.value.details["response_shape"]["type"] == "object"
    error = raised.value.details["validation_errors"][0]
    assert error == {
        "field": "$.tasks[0].acceptance_criteria",
        "expected": "array",
        "received": {"type": "string", "length": len(secret_raw_value)},
        "error_type": "list_type",
    }
    assert secret_raw_value not in str(raised.value.diagnostics())


async def test_gemini_quota_is_normalized_without_provider_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            json={
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "quota exhausted for secret tenant data",
                }
            },
        )

    provider = GeminiModelProvider("secret", 1, base_url="https://gemini.invalid", paid=False)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(
            ModelRequest(
                model="gemini-test",
                system_prompt="system",
                user_prompt="user",
                response_model=BaseAgentResult,
            )
        )
    await provider.client.aclose()
    assert raised.value.code == "MODEL_QUOTA_EXHAUSTED"
    assert "secret" not in raised.value.message


async def test_gemini_native_tool_call_and_function_response_round_trip() -> None:
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            schema = payload["tools"][0]["functionDeclarations"][0][
                "parametersJsonSchema"
            ]
            assert schema["type"] == "object"
            assert "title" not in schema
            assert "default" not in schema["properties"]["path"]
            assert "maxLength" not in schema["properties"]["path"]
            return httpx.Response(
                200,
                request=request,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": "call-1",
                                                "name": "forge__read_test_file",
                                            "args": {"path": "fixture.txt"},
                                        },
                                        "thoughtSignature": "opaque-signature",
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ]
                },
            )
        contents = payload["contents"]
        assert contents[0]["parts"][0]["text"] == "Read the fixture."
        assert contents[1]["role"] == "model"
        assert contents[1]["parts"][0]["thoughtSignature"] == "opaque-signature"
        function_response = contents[2]["parts"][0]["functionResponse"]
        assert function_response == {
            "id": "call-1",
            "name": "forge__read_test_file",
            "response": {"status": "success", "result": {"content": "fixture-ok"}},
        }
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": '{"turn":{"type":"final","result":'
                                    '{"status":"completed","summary":"fixture-ok",'
                                    '"output":{"artifacts":[],"details":[]},"notes":[]}}}'
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    provider = GeminiModelProvider(
        "test-secret", 1, base_url="https://gemini.invalid/v1beta", paid=False
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid/v1beta/",
        transport=httpx.MockTransport(handler),
        headers={"x-goog-api-key": "test-secret"},
    )
    tool = ModelTool(
        name="read_test_file",
        description="Read a harmless fixture.",
        input_model=FilesystemPathInput,
    )
    first = await provider.generate(
        ModelRequest(
            model="gemini-test",
            system_prompt="Use the tool.",
            user_prompt="Read the fixture.",
            response_model=AgentTurnResponse,
            tools=(tool,),
        )
    )
    assert first.output == {
        "type": "tool_call",
        "tool_name": "read_test_file",
        "arguments": {"path": "fixture.txt"},
    }
    assert first.tool_call is not None
    final = await provider.generate(
        ModelRequest(
            model="gemini-test",
            system_prompt="Use the tool.",
            user_prompt="This rebuilt prompt must not replace conversation history.",
            conversation_start_prompt="Read the fixture.",
            response_model=AgentTurnResponse,
            tools=(tool,),
            tool_exchanges=(
                ModelToolExchange(
                    call=first.tool_call,
                    response={"status": "success", "result": {"content": "fixture-ok"}},
                ),
            ),
        )
    )
    await provider.client.aclose()
    assert isinstance(final.output, AgentTurnResponse)
    assert final.output.turn.type == "final"
    assert len(requests) == 2


async def test_gemini_tool_protocol_error_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "function call is missing a thought signature",
                }
            },
        )

    provider = GeminiModelProvider(
        "secret", 1, base_url="https://gemini.invalid/v1beta", paid=False
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="https://gemini.invalid/v1beta/", transport=httpx.MockTransport(handler)
    )
    call = ModelToolCall(
        name="read_test_file",
        arguments={"path": "fixture.txt"},
        call_id="call-1",
        provider_context={
            "gemini_content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "read_test_file",
                            "args": {"path": "fixture.txt"},
                        }
                    }
                ],
            }
        },
    )
    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(
            ModelRequest(
                model="gemini-test",
                system_prompt="system",
                user_prompt="user",
                tools=(
                    ModelTool(
                        name="read_test_file",
                        description="Read a fixture.",
                        input_model=FilesystemPathInput,
                    ),
                ),
                tool_exchanges=(
                    ModelToolExchange(call=call, response={"status": "success"}),
                ),
            )
        )
    await provider.client.aclose()
    assert raised.value.code == "PROVIDER_TOOL_PROTOCOL_ERROR"
    assert "signature" not in raised.value.message.lower()


def test_free_gemini_developer_profile_requires_honest_tool_capabilities() -> None:
    developer = profile(
        "developer_coding",
        provider="gemini",
        capabilities=frozenset(
            {
                ModelCapability.TEXT,
                ModelCapability.CODING,
                ModelCapability.TOOL_CALLING,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        ),
    )
    selection = ModelRouter([developer]).select(
        context(
            required_capabilities=required_capabilities(
                role="DEVELOPER", has_tools=True, structured_output=True
            ),
            paid_budget_remaining=0,
            requested_alias="developer_coding",
        )
    )
    assert selection.profile.provider == "gemini"
    assert selection.profile.tier == EconomicTier.FREE
    assert selection.profile.paid is False
