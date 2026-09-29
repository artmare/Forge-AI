import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent_runtime.contracts import (
    BaseAgentResult,
    ModelRequest,
    ModelTool,
    ModelToolCall,
    ModelToolExchange,
    ProviderCallError,
)
from app.agent_runtime.providers import OpenRouterModelProvider
from app.tool_system.filesystem import FilesystemWriteInput


class RecordingCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _usage() -> Any:
    return SimpleNamespace(
        prompt_tokens=21,
        completion_tokens=9,
        total_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
    )


def _tool_response() -> Any:
    return SimpleNamespace(
        id="gen_tool_1",
        model="openrouter/test",
        usage=_usage(),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_write_1",
                            type="function",
                            function=SimpleNamespace(
                                name="forge__filesystem__write",
                                arguments=json.dumps(
                                    {
                                        "path": "src/app.py",
                                        "content": "print('forge')\n",
                                    }
                                ),
                            ),
                        )
                    ],
                )
            )
        ],
    )


def _final_response() -> Any:
    return SimpleNamespace(
        id="gen_final_1",
        model="openrouter/test",
        usage=_usage(),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=None,
                    content=json.dumps(
                        {
                            "status": "completed",
                            "summary": "Implemented and verified the requested change.",
                            "output": {
                                "artifacts": ["src/app.py"],
                                "details": ["Forge executed the filesystem write."],
                            },
                            "notes": [],
                        }
                    ),
                )
            )
        ],
    )


def _request(**kwargs: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "model": "openrouter/test",
        "system_prompt": "system",
        "user_prompt": "current user prompt",
        "conversation_start_prompt": "original user prompt",
        "response_model": BaseAgentResult,
        "tools": (
            ModelTool(
                name="filesystem.write",
                description="Write a scoped project file.",
                input_model=FilesystemWriteInput,
            ),
        ),
    }
    values.update(kwargs)
    return ModelRequest(**values)


async def test_openrouter_provider_returns_native_tool_call_without_executing_it() -> None:
    completions = RecordingCompletions([_tool_response()])
    provider = OpenRouterModelProvider(
        "test-key",
        30,
        base_url="https://openrouter.ai/api/v1",
        paid=False,
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await provider.generate(_request())

    assert result.output == {
        "type": "tool_call",
        "tool_name": "filesystem.write",
        "arguments": {
            "path": "src/app.py",
            "content": "print('forge')\n",
        },
    }
    assert result.tool_call == ModelToolCall(
        name="filesystem.write",
        arguments={"path": "src/app.py", "content": "print('forge')\n"},
        call_id="call_write_1",
        provider_context={"native_name": "forge__filesystem__write"},
    )
    assert result.usage.total_tokens == 30
    assert result.usage.cached_input_tokens == 4

    call = completions.calls[0]
    assert call["parallel_tool_calls"] is False
    assert call["tool_choice"] == "auto"
    assert call["tools"][0]["function"]["name"] == "forge__filesystem__write"
    assert call["tools"][0]["function"]["strict"] is True
    assert call["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original user prompt"},
    ]
    assert call["response_format"]["type"] == "json_schema"


async def test_openrouter_free_only_adds_zero_price_provider_ceiling() -> None:
    completions = RecordingCompletions([_tool_response()])
    provider = OpenRouterModelProvider(
        "test-key",
        30,
        base_url="https://openrouter.ai/api/v1",
        paid=False,
        free_only=True,
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    await provider.generate(_request())

    assert completions.calls[0]["extra_body"] == {
        "provider": {"max_price": {"prompt": 0, "completion": 0}}
    }


async def test_openrouter_provider_replays_tool_exchange_for_continuation() -> None:
    completions = RecordingCompletions([_final_response()])
    provider = OpenRouterModelProvider(
        "test-key",
        30,
        base_url="https://openrouter.ai/api/v1",
        paid=False,
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    tool_call = ModelToolCall(
        name="filesystem.write",
        arguments={"path": "src/app.py", "content": "print('forge')\n"},
        call_id="call_write_1",
        provider_context={"native_name": "forge__filesystem__write"},
    )

    result = await provider.generate(
        _request(
            tool_exchanges=(
                ModelToolExchange(
                    call=tool_call,
                    response={
                        "status": "success",
                        "tool": "filesystem.write",
                        "result": {"path": "src/app.py"},
                    },
                ),
            )
        )
    )

    assert isinstance(result.output, BaseAgentResult)
    assert result.output.status == "completed"
    assert result.output.output["artifacts"] == ["src/app.py"]

    messages = completions.calls[0]["messages"]
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["id"] == "call_write_1"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "forge__filesystem__write"
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_write_1"
    assert json.loads(messages[3]["content"])["status"] == "success"


async def test_openrouter_provider_rejects_multiple_tool_calls() -> None:
    response = _tool_response()
    response.choices[0].message.tool_calls.append(
        SimpleNamespace(
            id="call_write_2",
            type="function",
            function=SimpleNamespace(
                name="forge__filesystem__write",
                arguments=json.dumps({"path": "other.py", "content": "pass\n"}),
            ),
        )
    )
    completions = RecordingCompletions([response])
    provider = OpenRouterModelProvider(
        "test-key",
        30,
        base_url="https://openrouter.ai/api/v1",
        paid=False,
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(_request())

    assert raised.value.code == "INVALID_MODEL_OUTPUT"


async def test_openrouter_provider_requires_continuation_identity() -> None:
    completions = RecordingCompletions([_final_response()])
    provider = OpenRouterModelProvider(
        "test-key",
        30,
        base_url="https://openrouter.ai/api/v1",
        paid=False,
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(
            _request(
                tool_exchanges=(
                    ModelToolExchange(
                        call=ModelToolCall(
                            name="filesystem.write",
                            arguments={"path": "src/app.py", "content": "pass\n"},
                        ),
                        response={"status": "success"},
                    ),
                )
            )
        )

    assert raised.value.code == "PROVIDER_TOOL_PROTOCOL_ERROR"
    assert completions.calls == []


async def test_openrouter_provider_omits_unsupported_optional_parameters() -> None:
    completions = RecordingCompletions([_tool_response()])
    provider = OpenRouterModelProvider(
        "test-key", 30, base_url="https://openrouter.ai/api/v1", paid=False
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    await provider.generate(
        _request(metadata={"provider_supported_parameters": "tools,tool_choice"})
    )

    call = completions.calls[0]
    assert "response_format" not in call
    assert "parallel_tool_calls" not in call
    assert call["tool_choice"] == "auto"


@pytest.mark.parametrize(
    ("native_name", "arguments", "expected_code"),
    [
        ("forge__unknown", "{}", "INVALID_MODEL_OUTPUT"),
        ("forge__filesystem__write", "{not-json", "INVALID_MODEL_OUTPUT"),
    ],
)
async def test_openrouter_provider_rejects_invalid_native_tool_requests(
    native_name: str, arguments: str, expected_code: str
) -> None:
    response = _tool_response()
    response.choices[0].message.tool_calls[0].function.name = native_name
    response.choices[0].message.tool_calls[0].function.arguments = arguments
    completions = RecordingCompletions([response])
    provider = OpenRouterModelProvider(
        "test-key", 30, base_url="https://openrouter.ai/api/v1", paid=False
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(ProviderCallError) as raised:
        await provider.generate(_request())

    assert raised.value.code == expected_code
