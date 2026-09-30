import asyncio
import hashlib
import json

from pydantic import BaseModel, ValidationError

from app.tool_system.contracts import (
    GIT_TOOL_REFERENCES,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from app.tool_system.errors import ToolSystemError


class ToolExecutor:
    @staticmethod
    def validate_arguments(definition: ToolDefinition, arguments: dict[str, object]) -> BaseModel:
        try:
            return definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            schema = definition.input_model.model_json_schema()
            issues = json.loads(
                json.dumps(
                    exc.errors(include_url=False, include_input=False),
                    default=str,
                )
            )
            invalid_fields = sorted({str(item["loc"][0]) for item in issues if item.get("loc")})
            required = [str(item) for item in schema.get("required", [])]
            provided = sorted(str(item) for item in arguments)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"tool": definition.name, "arguments": arguments},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
            suggested_tool = None
            suggested_arguments: dict[str, object] | None = None
            if definition.name == "development.execute":
                action = arguments.get("action")
                git_tools = {item.value: tool for tool, item in GIT_TOOL_REFERENCES.items()}
                if isinstance(action, str) and action in git_tools:
                    suggested_tool = git_tools[action]
                    suggested_arguments = {}
            raise ToolSystemError(
                "TOOL_ARGUMENT_VALIDATION_FAILED",
                "Tool arguments do not match the required schema",
                details={
                    "tool_name": definition.name,
                    "invalid_call_fingerprint": fingerprint,
                    "required_fields": required,
                    "provided_fields": provided,
                    "missing_fields": sorted(set(required) - set(provided)),
                    "invalid_fields": invalid_fields,
                    "validation_errors": issues[:8],
                    "schema": schema,
                    "suggested_tool": suggested_tool,
                    "suggested_arguments": suggested_arguments,
                    "repair_instruction": (
                        "Repair this tool call only. Use exactly the declared fields and allowed "
                        "values; do not describe or execute the action in prose."
                    ),
                },
            ) from exc

    async def execute(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                raw_output = await definition.handler(arguments, context)
            output = definition.output_model.model_validate(raw_output)
            return ToolResult.success(output.model_dump(mode="json"))
        except TimeoutError:
            return ToolResult.failure("TOOL_TIMEOUT", "Tool execution timed out")
        except ToolSystemError as exc:
            return ToolResult.failure(exc.code, exc.message, details=exc.details)
        except ValidationError:
            return ToolResult.failure(
                "TOOL_OUTPUT_VALIDATION_FAILED",
                "Tool output did not match the registered schema",
            )
        except Exception:
            return ToolResult.failure("TOOL_EXECUTION_FAILED", "Tool execution failed")
