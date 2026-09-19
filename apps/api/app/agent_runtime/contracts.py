from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

AgentResultOutput = Annotated[
    dict[str, Any],
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "artifacts": {"type": "array", "items": {"type": "string"}},
                "details": {"type": "array", "items": {"type": "string"}},
                "execution_claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "FILE_MUTATION",
                                    "COMMAND",
                                    "TEST",
                                    "GIT",
                                    "BROWSER",
                                ],
                            },
                            "reference": {"type": ["string", "null"]},
                        },
                        "required": ["kind", "reference"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["artifacts", "details", "execution_claims"],
            "additionalProperties": False,
        }
    ),
]


class BaseAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"]
    summary: str = Field(min_length=1)
    output: AgentResultOutput
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ModelTool:
    """Provider-neutral description of one Forge-controlled model tool."""

    name: str
    description: str
    input_model: type[BaseModel]


@dataclass(frozen=True)
class ModelToolCall:
    """Provider-neutral tool request plus opaque in-process continuation state."""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None
    provider_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelToolExchange:
    call: ModelToolCall
    response: dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system_prompt: str
    user_prompt: str
    metadata: dict[str, str] = field(default_factory=dict)
    response_model: type[BaseModel] = BaseAgentResult
    tools: tuple[ModelTool, ...] = ()
    conversation_start_prompt: str | None = None
    tool_exchanges: tuple[ModelToolExchange, ...] = ()


@dataclass(frozen=True)
class ModelResponse:
    output: Any
    usage: ModelUsage = field(default_factory=ModelUsage)
    provider: str | None = None
    model: str | None = None
    response_id: str | None = None
    tool_call: ModelToolCall | None = None


class ModelProvider(Protocol):
    name: str
    paid: bool

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class ProviderCallError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        category: str = "PROVIDER",
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.category = category
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.provider_error_type = provider_error_type
        self.request_id = request_id
        self.exception_type = exception_type
        self.details = details or {}

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded provider metadata that is safe to persist internally."""
        values: dict[str, str | int | bool | None] = {
            "category": self.category,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "provider_error_type": self.provider_error_type,
            "request_id": self.request_id,
            "exception_type": self.exception_type,
        }
        diagnostics: dict[str, Any] = {
            key: value for key, value in values.items() if value is not None
        }
        safe_details = self.safe_details()
        if safe_details:
            diagnostics["details"] = safe_details
        return diagnostics

    def safe_details(self) -> dict[str, Any]:
        """Discard raw provider data and retain only bounded response-shape evidence."""
        safe: dict[str, Any] = {}
        for key in ("finish_reason", "candidate_count", "content_exists"):
            value = self.details.get(key)
            if isinstance(value, str | int | bool):
                safe[key] = value

        shape = self.details.get("response_shape")
        if isinstance(shape, dict):
            safe_shape: dict[str, Any] = {}
            if isinstance(shape.get("type"), str):
                safe_shape["type"] = str(shape["type"])[:40]
            if isinstance(shape.get("text_length"), int):
                safe_shape["text_length"] = shape["text_length"]
            top_level = shape.get("top_level_fields")
            if isinstance(top_level, list):
                safe_shape["top_level_fields"] = [
                    str(item)[:80] for item in top_level[:50] if isinstance(item, str)
                ]
            if safe_shape:
                safe["response_shape"] = safe_shape

        validation_errors = self.details.get("validation_errors")
        if isinstance(validation_errors, list):
            safe_errors: list[dict[str, Any]] = []
            for item in validation_errors[:20]:
                if not isinstance(item, dict):
                    continue
                evidence: dict[str, Any] = {}
                for key in ("field", "expected", "error_type"):
                    if isinstance(item.get(key), str):
                        evidence[key] = str(item[key])[:500]
                received = item.get("received")
                if isinstance(received, dict):
                    received_shape: dict[str, Any] = {}
                    if isinstance(received.get("type"), str):
                        received_shape["type"] = str(received["type"])[:40]
                    if isinstance(received.get("length"), int):
                        received_shape["length"] = received["length"]
                    if received_shape:
                        evidence["received"] = received_shape
                if evidence:
                    safe_errors.append(evidence)
            if safe_errors:
                safe["validation_errors"] = safe_errors
        return safe
