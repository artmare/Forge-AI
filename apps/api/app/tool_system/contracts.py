from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from app.agent_runtime.contracts import BaseAgentResult
from app.domain.enums import ToolRiskLevel

ToolArguments = Annotated[
    dict[str, Any],
    WithJsonSchema(
        {
            "type": "object",
            "properties": {
                "path": {"type": ["string", "null"]},
                "content": {"type": ["string", "null"]},
                "action": {
                    "type": ["string", "null"],
                    "enum": [
                        "NODE_TEST",
                        "NODE_BUILD",
                        "NODE_LINT",
                        "NODE_TYPECHECK",
                        "PYTHON_TEST",
                        "PYTHON_LINT",
                        None,
                    ],
                },
                "target": {"type": ["string", "null"]},
            },
            "required": ["path", "content", "action", "target"],
            "additionalProperties": False,
        }
    ),
]

_TOOL_ARGUMENT_KEYS: dict[str, frozenset[str]] = {
    "filesystem.list": frozenset({"path"}),
    "filesystem.read": frozenset({"path"}),
    "filesystem.write": frozenset({"path", "content"}),
    "development.execute": frozenset({"action", "target"}),
    "development.install_dependencies": frozenset(),
    "git.init": frozenset(),
    "git.status": frozenset(),
    "git.diff": frozenset(),
    "git.log": frozenset(),
    "git.commit": frozenset(),
}


class ToolErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "error"]
    result: dict[str, Any] | None = None
    error: ToolErrorPayload | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ToolResult:
        if self.status == "success" and (self.result is None or self.error is not None):
            raise ValueError("Successful tool results require result and prohibit error")
        if self.status == "error" and (self.error is None or self.result is not None):
            raise ValueError("Failed tool results require error and prohibit result")
        return self

    @classmethod
    def success(cls, result: dict[str, Any]) -> ToolResult:
        return cls(status="success", result=result)

    @classmethod
    def failure(cls, code: str, message: str) -> ToolResult:
        return cls(status="error", error=ToolErrorPayload(code=code, message=message))


class ToolRequestTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: ToolArguments

    @model_validator(mode="after")
    def normalize_arguments_for_selected_tool(self) -> ToolRequestTurn:
        """Discard only known union-schema fields that cannot belong to the selected tool.

        OpenAI strict structured outputs require every property in the shared argument object to
        be present and nullable. A provider may therefore populate a valid field for another tool
        (for example ``action`` on ``filesystem.write``). The selected ToolDefinition remains the
        authority: normalize the shared wire shape before its strict per-tool Pydantic validation.
        """
        arguments = {key: item for key, item in self.arguments.items() if item is not None}
        allowed = _TOOL_ARGUMENT_KEYS.get(self.tool_name)
        if allowed is not None:
            arguments = {key: item for key, item in arguments.items() if key in allowed}
        self.arguments = arguments
        return self


class FinalTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["final"]
    result: BaseAgentResult


AgentTurn = ToolRequestTurn | FinalTurn


class AgentTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: AgentTurn


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: UUID
    tool: str
    status: Literal["success", "error"]
    result: dict[str, Any] | None = None
    error: ToolErrorPayload | None = None
    truncated: bool = False
    original_chars: int | None = None


class ToolDefinitionPublic(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: ToolRiskLevel
    permission_required: str
    timeout_seconds: float
    enabled: bool


@dataclass(frozen=True)
class ToolExecutionContext:
    company_id: UUID
    project_id: UUID | None
    task_id: UUID
    task_run_id: UUID
    agent_id: UUID
    agent_run_id: UUID


ToolHandler = Callable[[BaseModel, ToolExecutionContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk_level: ToolRiskLevel
    permission_required: str
    timeout_seconds: float
    enabled: bool
    handler: ToolHandler

    def public(self) -> ToolDefinitionPublic:
        return ToolDefinitionPublic(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            risk_level=self.risk_level,
            permission_required=self.permission_required,
            timeout_seconds=self.timeout_seconds,
            enabled=self.enabled,
        )
