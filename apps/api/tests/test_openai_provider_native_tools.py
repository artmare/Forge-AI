import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AuthenticationError, BadRequestError, InternalServerError, RateLimitError

from app.agent_runtime.contracts import (
    BaseAgentResult,
    ModelRequest,
    ModelTool,
    ProviderCallError,
)
from app.agent_runtime.providers import OpenAIModelProvider
from app.tool_system.filesystem import FilesystemWriteInput


class RecordingResponses:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class RaisingResponses:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def parse(self, **_kwargs: Any) -> Any:
        raise self.error


def _response(*, output: list[Any], output_parsed: Any = None) -> Any:
    return SimpleNamespace(
        id="resp_native_tool_test",
        output=output,
        output_parsed=output_parsed,
        usage=SimpleNamespace(
            input_tokens=17,
            output_tokens=11,
            total_tokens=28,
            input_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
    )


async def test_openai_provider_uses_strict_native_function_for_forge_tool() -> None:
    tool_call = SimpleNamespace(
        type="function_call",
        name="forge__filesystem__write",
        arguments=json.dumps(
            {
                "path": "src/clip.mjs",
                "content": "export const ready = true;\n",
            }
        ),
    )
    responses = RecordingResponses(_response(output=[tool_call]))
    provider = OpenAIModelProvider("test-key", 30)
    provider.client = SimpleNamespace(responses=responses)

    result = await provider.generate(
        ModelRequest(
            model="gpt-test",
            system_prompt="system",
            user_prompt="user",
            tools=(
                ModelTool(
                    name="filesystem.write",
                    description="Write a scoped project file.",
                    input_model=FilesystemWriteInput,
                ),
            ),
        )
    )

    assert result.output == {
        "type": "tool_call",
        "tool_name": "filesystem.write",
        "arguments": {
            "path": "src/clip.mjs",
            "content": "export const ready = true;\n",
        },
    }
    assert result.usage.total_tokens == 28
    call = responses.calls[0]
    assert call["text_format"] is BaseAgentResult
    assert call["parallel_tool_calls"] is False
    assert call["max_tool_calls"] == 1
    assert call["tools"][0]["function"]["name"] == "forge__filesystem__write"
    assert call["tools"][0]["function"]["strict"] is True
    assert set(call["tools"][0]["function"]["parameters"]["properties"]) == {
        "path",
        "content",
    }


async def test_openai_provider_accepts_final_result_when_native_tools_are_available() -> None:
    final = BaseAgentResult(
        status="completed",
        summary="Project scaffold created and verified.",
        output={"artifacts": ["package.json"], "details": ["Tests passed."]},
    )
    responses = RecordingResponses(_response(output=[], output_parsed=final))
    provider = OpenAIModelProvider("test-key", 30)
    provider.client = SimpleNamespace(responses=responses)

    result = await provider.generate(
        ModelRequest(
            model="gpt-test",
            system_prompt="system",
            user_prompt="user",
            tools=(
                ModelTool(
                    name="filesystem.write",
                    description="Write a scoped project file.",
                    input_model=FilesystemWriteInput,
                ),
            ),
        )
    )

    assert result.output == final


def _status_error(error_type: type[Exception], status: int, body: dict[str, str]) -> Exception:
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        headers={"x-request-id": "req_safe_test"},
    )
    return error_type("safe test error", response=response, body=body)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("error", "code", "retryable", "category"),
    [
        (
            _status_error(
                RateLimitError,
                429,
                {"type": "rate_limit_error", "code": "rate_limit_exceeded"},
            ),
            "MODEL_RATE_LIMIT",
            True,
            "RATE_LIMIT",
        ),
        (
            _status_error(
                RateLimitError,
                429,
                {"type": "insufficient_quota", "code": "credit_balance_exhausted"},
            ),
            "MODEL_QUOTA_EXHAUSTED",
            False,
            "QUOTA",
        ),
        (
            _status_error(InternalServerError, 503, {"type": "server_error"}),
            "MODEL_PROVIDER_TEMPORARY",
            True,
            "TEMPORARY_PROVIDER",
        ),
        (
            _status_error(AuthenticationError, 401, {"type": "authentication_error"}),
            "MODEL_AUTH_ERROR",
            False,
            "AUTHENTICATION",
        ),
        (
            _status_error(BadRequestError, 400, {"type": "invalid_request_error"}),
            "MODEL_CONFIGURATION_ERROR",
            False,
            "CONFIGURATION",
        ),
    ],
)
async def test_openai_provider_preserves_safe_failure_classification(
    error: Exception,
    code: str,
    retryable: bool,
    category: str,
) -> None:
    provider = OpenAIModelProvider("test-key", 30)
    provider.client = SimpleNamespace(responses=RaisingResponses(error))

    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(
            ModelRequest(model="gpt-test", system_prompt="system", user_prompt="user")
        )

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert raised.value.category == category
    assert raised.value.http_status in {400, 401, 429, 503}
    assert raised.value.request_id == "req_safe_test"
