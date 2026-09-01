from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.contracts import RunnerRequest, RunnerResponse
from app.development.profile import DevelopmentProfileService
from app.development.registry import CommandRegistry
from app.development.runner_client import RunnerClient, runner_client_from_settings
from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus, TaskStatus
from app.domain.models import DevelopmentExecution, Task
from app.services.event_factory import EventFactory
from app.tool_system.contracts import ToolExecutionContext
from app.tool_system.errors import ToolSystemError


class DevelopmentExecutionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        runner: RunnerClient | None = None,
        registry: CommandRegistry | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.runner = runner or runner_client_from_settings(self.settings)
        self.registry = registry or CommandRegistry(self.settings)
        self.profiles = DevelopmentProfileService(session, self.settings)
        self.events = EventFactory(session)

    async def execute(
        self,
        action: DevelopmentAction,
        raw_arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> DevelopmentExecution:
        definition = self.registry.get(action)
        if definition is None:
            raise ToolSystemError(
                "DEVELOPMENT_ACTION_UNKNOWN", "Development action is not registered"
            )
        if not definition.enabled or not self.settings.development_enabled:
            raise ToolSystemError("DEVELOPMENT_ACTION_DISABLED", "Development action is disabled")
        try:
            arguments = definition.validate_arguments(raw_arguments)
        except ValueError as exc:
            raise ToolSystemError(
                "DEVELOPMENT_ARGUMENT_VALIDATION_FAILED", "Development arguments are not allowed"
            ) from exc
        task = await self.session.get(Task, context.task_id)
        if (
            task is None
            or task.status != TaskStatus.IN_PROGRESS
            or task.project_id is None
            or task.project_id != context.project_id
            or task.company_id != context.company_id
        ):
            raise ToolSystemError("DEVELOPMENT_STATE_CONFLICT", "Task is not executable")
        count = await self.session.scalar(
            select(func.count())
            .select_from(DevelopmentExecution)
            .where(
                DevelopmentExecution.task_run_id == context.task_run_id,
                DevelopmentExecution.agent_id == context.agent_id,
            )
        )
        if int(count or 0) >= self.settings.development_max_executions:
            raise ToolSystemError(
                "DEVELOPMENT_EXECUTION_LIMIT", "Development execution limit was reached"
            )
        profile = await self.profiles.detect(task.project_id, commit=False)
        if profile.project_type not in definition.project_types:
            raise ToolSystemError(
                "DEVELOPMENT_PROFILE_MISMATCH",
                f"{action.value} is unavailable for {profile.project_type.value} projects",
            )
        configured_actions = {
            profile.install_action,
            profile.test_action,
            profile.build_action,
            profile.lint_action,
            profile.typecheck_action,
        }
        if (
            action
            in {
                DevelopmentAction.NODE_TEST,
                DevelopmentAction.NODE_BUILD,
                DevelopmentAction.NODE_LINT,
                DevelopmentAction.NODE_TYPECHECK,
            }
            and action not in configured_actions
        ):
            raise ToolSystemError(
                "DEVELOPMENT_ACTION_UNSUPPORTED",
                f"{action.value} is unavailable because package.json does not declare its script",
            )
        working_directory = f"{task.company_id}/{task.project_id}"
        execution = DevelopmentExecution(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            task_run_id=context.task_run_id,
            agent_run_id=context.agent_run_id,
            agent_id=context.agent_id,
            action=action,
            status=DevelopmentExecutionStatus.REQUESTED,
            working_directory=working_directory,
            safe_arguments=arguments,
            timeout_seconds=definition.timeout_seconds,
            network_enabled=definition.network_enabled,
            correlation_id=task.id,
        )
        self.session.add(execution)
        await self.session.flush()
        await self._event(
            execution, "DEVELOPMENT_EXECUTION_REQUESTED", "Development action requested."
        )
        execution.status = DevelopmentExecutionStatus.AUTHORIZED
        await self.session.commit()

        execution.status = DevelopmentExecutionStatus.RUNNING
        execution.started_at = datetime.now(UTC)
        await self._event(execution, "DEVELOPMENT_EXECUTION_STARTED", "Development action started.")
        await self.session.commit()
        try:
            response = await self.runner.execute(
                RunnerRequest(
                    request_id=execution.id,
                    action=action,
                    workspace_relative=working_directory,
                    safe_arguments=arguments,
                    timeout_seconds=definition.timeout_seconds,
                    output_limit_bytes=definition.output_limit_bytes,
                    task_id=task.id,
                )
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_cancelled(execution.id))
            raise
        completed = await self._complete(execution.id, response)
        profile = await self.profiles.detect(task.project_id, commit=False)
        if completed.status == DevelopmentExecutionStatus.SUCCEEDED:
            if action == DevelopmentAction.GIT_INIT:
                profile.repository_initialized = True
                profile.repository_branch = "main"
            elif action == DevelopmentAction.GIT_CHECKPOINT:
                profile.repository_initialized = True
                profile.initial_checkpoint_created = True
            elif action == DevelopmentAction.GIT_STATUS:
                profile.repository_initialized = True
                profile.changed_files_count = int((completed.change_summary or {}).get("total", 0))
        await self.session.commit()
        return completed

    async def _mark_cancelled(self, execution_id: UUID) -> None:
        execution = await self.session.scalar(
            select(DevelopmentExecution)
            .where(DevelopmentExecution.id == execution_id)
            .with_for_update()
        )
        if execution is None or execution.status != DevelopmentExecutionStatus.RUNNING:
            await self.session.rollback()
            return
        execution.status = DevelopmentExecutionStatus.CANCELLED
        execution.finished_at = datetime.now(UTC)
        execution.error_code = "DEVELOPMENT_EXECUTION_CANCELLED"
        execution.error_message = "Development action was cancelled."
        await self._event(
            execution,
            "DEVELOPMENT_EXECUTION_FAILED",
            "Development action cancelled.",
        )
        await self.session.commit()

    async def _complete(self, execution_id: UUID, response: RunnerResponse) -> DevelopmentExecution:
        execution = await self.session.scalar(
            select(DevelopmentExecution)
            .where(DevelopmentExecution.id == execution_id)
            .with_for_update()
        )
        if execution is None or execution.status != DevelopmentExecutionStatus.RUNNING:
            await self.session.rollback()
            raise ToolSystemError("DEVELOPMENT_STATE_CONFLICT", "Execution is no longer running")
        execution.status = response.status
        execution.exit_code = response.exit_code
        execution.stdout_excerpt = response.stdout_excerpt
        execution.stderr_excerpt = response.stderr_excerpt
        execution.stdout_bytes = response.stdout_bytes
        execution.stderr_bytes = response.stderr_bytes
        execution.output_truncated = response.truncated
        execution.duration_ms = Decimal(str(round(response.duration_ms, 2)))
        execution.error_code = response.error_code
        execution.error_message = response.error_message
        execution.finished_at = datetime.now(UTC)
        if execution.action == DevelopmentAction.GIT_STATUS:
            execution.change_summary = self._parse_porcelain(response.stdout_excerpt)
        event_type = {
            DevelopmentExecutionStatus.SUCCEEDED: "DEVELOPMENT_EXECUTION_SUCCEEDED",
            DevelopmentExecutionStatus.TIMED_OUT: "DEVELOPMENT_EXECUTION_TIMED_OUT",
            DevelopmentExecutionStatus.CANCELLED: "DEVELOPMENT_EXECUTION_FAILED",
        }.get(execution.status, "DEVELOPMENT_EXECUTION_FAILED")
        await self._event(
            execution, event_type, f"Development action {execution.status.value.lower()}."
        )
        if (
            execution.action == DevelopmentAction.GIT_CHECKPOINT
            and execution.status == DevelopmentExecutionStatus.SUCCEEDED
        ):
            await self._event(execution, "GIT_CHECKPOINT_CREATED", "Local task checkpoint created.")
        await self.session.commit()
        return execution

    async def list_for_task(self, task_id: UUID) -> list[DevelopmentExecution]:
        return list(
            await self.session.scalars(
                select(DevelopmentExecution)
                .where(DevelopmentExecution.task_id == task_id)
                .order_by(DevelopmentExecution.created_at, DevelopmentExecution.id)
            )
        )

    async def recover_stale(self, limit: int = 100) -> int:
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self.settings.development_execution_stale_seconds
        )
        rows = list(
            await self.session.scalars(
                select(DevelopmentExecution)
                .where(
                    DevelopmentExecution.status == DevelopmentExecutionStatus.RUNNING,
                    DevelopmentExecution.started_at < cutoff,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = DevelopmentExecutionStatus.FAILED
            row.finished_at = datetime.now(UTC)
            row.error_code = "DEVELOPMENT_EXECUTION_INTERRUPTED"
            row.error_message = "Runner completion was ambiguous after process interruption."
            await self._event(
                row, "DEVELOPMENT_EXECUTION_FAILED", "Stale development action recovered."
            )
        await self.session.commit()
        return len(rows)

    async def _event(self, execution: DevelopmentExecution, kind: str, message: str) -> None:
        await self.events.create(
            company_id=execution.company_id,
            project_id=execution.project_id,
            agent_id=execution.agent_id,
            task_id=execution.task_id,
            correlation_id=execution.correlation_id,
            event_type=kind,
            message=message,
            payload={
                "development_execution_id": str(execution.id),
                "action": execution.action.value,
                "status": execution.status.value,
                "exit_code": execution.exit_code,
                "output_truncated": execution.output_truncated,
            },
        )

    @staticmethod
    def _parse_porcelain(output: str) -> dict[str, Any]:
        files: list[dict[str, str]] = []
        counts = {"added": 0, "modified": 0, "deleted": 0}
        for line in output.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:]
            state = "modified"
            if "?" in code or "A" in code:
                state = "added"
            elif "D" in code:
                state = "deleted"
            counts[state] += 1
            files.append({"path": path, "status": state})
        return {**counts, "total": len(files), "files": files[:200]}
