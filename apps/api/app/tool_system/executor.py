import asyncio

from pydantic import BaseModel, ValidationError

from app.tool_system.contracts import ToolDefinition, ToolExecutionContext, ToolResult
from app.tool_system.errors import ToolSystemError


class ToolExecutor:
    @staticmethod
    def validate_arguments(definition: ToolDefinition, arguments: dict[str, object]) -> BaseModel:
        try:
            return definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolSystemError(
                "TOOL_ARGUMENT_VALIDATION_FAILED",
                "Tool arguments do not match the required schema",
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
            return ToolResult.failure(exc.code, exc.message)
        except ValidationError:
            return ToolResult.failure(
                "TOOL_OUTPUT_VALIDATION_FAILED",
                "Tool output did not match the registered schema",
            )
        except Exception:
            return ToolResult.failure("TOOL_EXECUTION_FAILED", "Tool execution failed")
