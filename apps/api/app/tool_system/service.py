import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.profile import DevelopmentProfileService
from app.domain.enums import TaskStatus, ToolCallStatus
from app.domain.models import ToolCall
from app.repositories.task import TaskRepository
from app.repositories.tool_call import ToolCallRepository
from app.services.event_factory import EventFactory
from app.tool_system.contracts import (
    ToolExecutionContext,
    ToolObservation,
    ToolRequestTurn,
    ToolResult,
)
from app.tool_system.errors import ToolSystemError
from app.tool_system.executor import ToolExecutor
from app.tool_system.permissions import PermissionEngine
from app.tool_system.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        registry: ToolRegistry | None = None,
        executor: ToolExecutor | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.registry = registry or ToolRegistry.from_settings(self.settings)
        self.executor = executor or ToolExecutor()
        self.permissions = PermissionEngine(session)
        self.calls = ToolCallRepository(session)
        self.tasks = TaskRepository(session)
        self.events = EventFactory(session)

    async def request_and_execute(
        self, request: ToolRequestTurn, context: ToolExecutionContext
    ) -> tuple[ToolCall, ToolObservation]:
        definition = self.registry.get(request.tool_name)
        call = await self.calls.add(
            ToolCall(
                agent_run_id=context.agent_run_id,
                task_run_id=context.task_run_id,
                task_id=context.task_id,
                agent_id=context.agent_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                permission=(definition.permission_required if definition else None),
            )
        )
        await self._event(call, "TOOL_CALL_REQUESTED", "Agent requested a tool call.")
        await self.session.commit()
        result = await self.execute_existing(call.id, context)
        completed = await self.calls.get(call.id)
        if completed is None:
            raise ToolSystemError("TOOL_EXECUTION_FAILED", "ToolCall was not found")
        return completed, self.observation(completed, result)

    async def execute_existing(self, call_id: UUID, context: ToolExecutionContext) -> ToolResult:
        call = await self.calls.get(call_id)
        if call is None:
            return ToolResult.failure("TOOL_NOT_FOUND", "ToolCall was not found")
        terminal = self._terminal_result(call)
        if terminal is not None:
            return terminal
        scoped_task = await self.tasks.get_current(call.task_id)
        if scoped_task is None:
            return await self._finish_before_execution(
                call_id, ToolCallStatus.FAILED, "TOOL_STATE_CONFLICT", "Task was not found"
            )
        if not self._context_matches(call, context, scoped_task):
            return await self._finish_before_execution(
                call_id, ToolCallStatus.DENIED, "TOOL_PERMISSION_DENIED", "Tool scope is invalid"
            )
        definition = self.registry.get(call.tool_name)
        if definition is None:
            return await self._finish_before_execution(
                call_id, ToolCallStatus.FAILED, "TOOL_NOT_FOUND", "Requested tool is not registered"
            )
        if not definition.enabled:
            return await self._finish_before_execution(
                call_id, ToolCallStatus.FAILED, "TOOL_DISABLED", "Requested tool is disabled"
            )
        try:
            validated = self.executor.validate_arguments(definition, call.arguments)
        except ToolSystemError as exc:
            return await self._finish_before_execution(
                call_id, ToolCallStatus.FAILED, exc.code, exc.message
            )

        if call.status == ToolCallStatus.REQUESTED:
            decision = await self.permissions.check(
                agent_id=call.agent_id,
                company_id=context.company_id,
                definition=definition,
                has_project_workspace=context.project_id is not None,
            )
            if not decision.allowed:
                status = (
                    ToolCallStatus.DENIED
                    if decision.code == "TOOL_PERMISSION_DENIED"
                    else ToolCallStatus.FAILED
                )
                return await self._finish_before_execution(
                    call_id, status, decision.code, decision.message
                )
            call = await self.calls.get_for_update(call_id)
            if call is None or call.status != ToolCallStatus.REQUESTED:
                await self.session.rollback()
                return ToolResult.failure("TOOL_STATE_CONFLICT", "ToolCall state changed")
            call.status = ToolCallStatus.AUTHORIZED
            await self._event(call, "TOOL_CALL_AUTHORIZED", "Tool call was authorized.")
            await self.session.commit()

        claimed = await self._claim(call_id)
        if claimed is None:
            current = await self.calls.get(call_id)
            terminal = self._terminal_result(current) if current else None
            return terminal or ToolResult.failure(
                "TOOL_ALREADY_RUNNING", "ToolCall has already been claimed"
            )
        started = perf_counter()
        result = await self.executor.execute(definition, validated, context)
        completed = await self._complete(claimed.id, result)
        if (
            completed.status == ToolCallStatus.SUCCEEDED
            and completed.tool_name == "filesystem.write"
            and context.project_id is not None
        ):
            try:
                profile = await DevelopmentProfileService(self.session, self.settings).detect(
                    context.project_id, commit=False
                )
                actions = [
                    action.value
                    for action in (
                        profile.test_action,
                        profile.build_action,
                        profile.lint_action,
                        profile.typecheck_action,
                    )
                    if action is not None
                ]
                refresh: dict[str, object] = {
                    "project_type": profile.project_type.value,
                    "package_manager": profile.package_manager.value,
                    "available_actions": actions,
                    "detection_source": profile.detection_source,
                }
                if profile.detection_source == "package.json (invalid JSON)":
                    refresh["warning"] = {
                        "code": "DEVELOPMENT_MANIFEST_INVALID",
                        "message": "package.json is not valid JSON and must be corrected.",
                    }
                if result.result is not None:
                    result.result["profile_refresh"] = refresh
                current = await self.calls.get_for_update(completed.id)
                if current is not None:
                    current.result = result.result
                    completed = current
                await self.session.commit()
            except Exception as exc:
                await self.session.rollback()
                logger.warning(
                    "Development profile refresh failed after a successful filesystem write",
                    extra={
                        "event": "development_profile_refresh_failed",
                        "tool_call_id": str(completed.id),
                        "project_id": str(context.project_id),
                        "error_type": type(exc).__name__,
                    },
                )
        logger.info(
            "Tool call completed",
            extra={
                "event": "tool_call_completed",
                "tool_call_id": str(completed.id),
                "agent_run_id": str(completed.agent_run_id),
                "agent_id": str(completed.agent_id),
                "task_id": str(completed.task_id),
                "tool_name": completed.tool_name,
                "status": completed.status.value,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return result

    async def _claim(self, call_id: UUID) -> ToolCall | None:
        call = await self.calls.get_for_update(call_id)
        if call is None or call.status != ToolCallStatus.AUTHORIZED:
            await self.session.rollback()
            return None
        task = await self.tasks.get(call.task_id)
        if task is None or task.status != TaskStatus.IN_PROGRESS:
            call.status = ToolCallStatus.CANCELLED
            call.error = {
                "code": "TASK_CANCELLED",
                "message": "Task is no longer executing",
            }
            call.completed_at = datetime.now(UTC)
            await self._event(call, "TOOL_CALL_FAILED", "Tool call was cancelled before execution.")
            await self.session.commit()
            return None
        call.status = ToolCallStatus.RUNNING
        call.started_at = datetime.now(UTC)
        await self._event(call, "TOOL_CALL_STARTED", "Tool execution started.")
        await self.session.commit()
        return call

    async def _complete(self, call_id: UUID, result: ToolResult) -> ToolCall:
        call = await self.calls.get_for_update(call_id)
        if call is None or call.status != ToolCallStatus.RUNNING:
            await self.session.rollback()
            raise ToolSystemError("TOOL_STATE_CONFLICT", "ToolCall is no longer running")
        call.completed_at = datetime.now(UTC)
        if result.status == "success":
            call.status = ToolCallStatus.SUCCEEDED
            call.result = result.result
            event_type = "TOOL_CALL_SUCCEEDED"
            message = "Tool execution succeeded."
        else:
            call.status = ToolCallStatus.FAILED
            call.error = result.error.model_dump(mode="json") if result.error else None
            event_type = "TOOL_CALL_FAILED"
            message = "Tool execution failed."
        await self._event(call, event_type, message)
        await self.session.commit()
        return call

    async def _finish_before_execution(
        self,
        call_id: UUID,
        status: ToolCallStatus,
        code: str,
        message: str,
    ) -> ToolResult:
        call = await self.calls.get_for_update(call_id)
        if call is None:
            await self.session.rollback()
            return ToolResult.failure("TOOL_NOT_FOUND", "ToolCall was not found")
        terminal = self._terminal_result(call)
        if terminal is not None:
            await self.session.rollback()
            return terminal
        if call.status == ToolCallStatus.RUNNING:
            await self.session.rollback()
            return ToolResult.failure("TOOL_ALREADY_RUNNING", "ToolCall has already been claimed")
        call.status = status
        call.error = {"code": code, "message": message}
        call.completed_at = datetime.now(UTC)
        event_type = "TOOL_CALL_DENIED" if status == ToolCallStatus.DENIED else "TOOL_CALL_FAILED"
        await self._event(call, event_type, message)
        await self.session.commit()
        return ToolResult.failure(code, message)

    def observation(self, call: ToolCall, result: ToolResult) -> ToolObservation:
        payload = result.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(serialized) <= self.settings.tool_observation_max_chars:
            return ToolObservation(
                tool_call_id=call.id,
                tool=call.tool_name,
                status=result.status,
                result=result.result,
                error=result.error,
            )
        if result.status == "success" and result.result is not None:
            limited = dict(result.result)
            content = limited.get("content")
            if isinstance(content, str):
                excess = len(serialized) - self.settings.tool_observation_max_chars
                limited["content"] = content[: max(len(content) - excess - 64, 0)]
            else:
                limited = {"preview": serialized[: self.settings.tool_observation_max_chars]}
            return ToolObservation(
                tool_call_id=call.id,
                tool=call.tool_name,
                status="success",
                result=limited,
                truncated=True,
                original_chars=len(serialized),
            )
        return ToolObservation(
            tool_call_id=call.id,
            tool=call.tool_name,
            status=result.status,
            error=result.error,
            truncated=True,
            original_chars=len(serialized),
        )

    async def _event(self, call: ToolCall, event_type: str, message: str) -> None:
        task = await self.tasks.get(call.task_id)
        if task is None:
            raise ToolSystemError("TOOL_STATE_CONFLICT", "ToolCall task was not found")
        path = call.arguments.get("path")
        payload: dict[str, object] = {
            "tool_call_id": str(call.id),
            "agent_run_id": str(call.agent_run_id),
            "task_run_id": str(call.task_run_id),
            "tool_name": call.tool_name,
            "status": call.status.value,
        }
        if isinstance(path, str):
            payload["path"] = path
        result_bytes = call.result.get("byte_size") if call.result else None
        if isinstance(result_bytes, int):
            payload["byte_count"] = result_bytes
        if call.error and isinstance(call.error.get("code"), str):
            payload["error_code"] = call.error["code"]
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=call.agent_id,
            task_id=task.id,
            event_type=event_type,
            message=message,
            payload=payload,
        )

    @staticmethod
    def _context_matches(call: ToolCall, context: ToolExecutionContext, task: Any) -> bool:
        return (
            call.agent_run_id == context.agent_run_id
            and call.task_run_id == context.task_run_id
            and call.task_id == context.task_id
            and call.agent_id == context.agent_id
            and task.id == call.task_id
            and task.company_id == context.company_id
            and task.project_id == context.project_id
            and task.assigned_agent_id == context.agent_id
        )

    @staticmethod
    def _terminal_result(call: ToolCall | None) -> ToolResult | None:
        if call is None:
            return None
        if call.status == ToolCallStatus.SUCCEEDED and call.result is not None:
            return ToolResult.success(call.result)
        if call.status in {
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.CANCELLED,
        }:
            error = call.error or {
                "code": "TOOL_EXECUTION_FAILED",
                "message": "Tool execution did not complete",
            }
            return ToolResult.failure(str(error["code"]), str(error["message"]))
        return None
