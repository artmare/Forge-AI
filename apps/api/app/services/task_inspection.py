from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentExecutionStatus,
    QADecision,
    TaskStatus,
    ToolCallStatus,
)
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import (
    AcceptanceVerification,
    Agent,
    AgentRun,
    DevelopmentExecution,
    ExecutionJob,
    ProjectDevelopmentProfile,
    QAResult,
    Task,
    TaskDependency,
    TaskReview,
    TaskRun,
    ToolCall,
)
from app.planning.dependency_resolver import DependencyResolver
from app.schemas.task_inspection import (
    InspectionAcceptance,
    InspectionAgent,
    InspectionAgentRun,
    InspectionCommand,
    InspectionDevelopmentProfile,
    InspectionError,
    InspectionExecutionJob,
    InspectionIteration,
    InspectionQAResult,
    InspectionReview,
    InspectionTaskLink,
    InspectionTaskRun,
    InspectionToolCall,
    TaskFailureSummary,
    TaskInspectionResponse,
)

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|database[_-]?url|"
    r"password|private[_-]?key|redis[_-]?url|secret|session|token)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{8,}=*"), r"\1 [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|cookie|credential|password|secret|token)"
            r"\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/]+):[^\s@/]+@"),
        r"\1:[REDACTED]@",
    ),
)
_CONTENT_KEYS = frozenset({"content", "file_content", "source", "body"})
_MAX_TEXT = 4_000


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    safe = value
    for pattern, replacement in _SECRET_PATTERNS:
        safe = pattern.sub(replacement, safe)
    if len(safe) > _MAX_TEXT:
        return f"{safe[:_MAX_TEXT]}\n[output truncated for inspection]"
    return safe


def _safe_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if key is not None and key.lower() in _CONTENT_KEYS and isinstance(value, str):
        return f"[content omitted: {len(value)} characters]"
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:200]]
    return value


def _error(code: str | None, message: str | None) -> InspectionError | None:
    if not code and not message:
        return None
    return InspectionError(
        code=code or "UNSPECIFIED_FAILURE",
        message=_safe_text(message) or "No durable error message was recorded.",
    )


class TaskInspectionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resolver = DependencyResolver(session)

    async def get(self, task_id: UUID) -> TaskInspectionResponse:
        task = await self.session.get(Task, task_id)
        if task is None:
            raise EntityNotFoundError("Task")

        task_runs = list(
            await self.session.scalars(
                select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.iteration)
            )
        )
        agent_runs = list(
            await self.session.scalars(
                select(AgentRun).where(AgentRun.task_id == task.id).order_by(AgentRun.created_at)
            )
        )
        commands = list(
            await self.session.scalars(
                select(DevelopmentExecution)
                .where(DevelopmentExecution.task_id == task.id)
                .order_by(DevelopmentExecution.created_at, DevelopmentExecution.id)
            )
        )
        qa_results = list(
            await self.session.scalars(
                select(QAResult).where(QAResult.task_id == task.id).order_by(QAResult.iteration)
            )
        )
        verifications = list(
            await self.session.scalars(
                select(AcceptanceVerification)
                .where(AcceptanceVerification.task_id == task.id)
                .order_by(AcceptanceVerification.iteration, AcceptanceVerification.criterion_index)
            )
        )
        tool_calls = list(
            await self.session.scalars(
                select(ToolCall).where(ToolCall.task_id == task.id).order_by(ToolCall.created_at)
            )
        )
        reviews = list(
            await self.session.scalars(
                select(TaskReview)
                .where(TaskReview.task_id == task.id)
                .order_by(TaskReview.iteration)
            )
        )
        execution_jobs = list(
            await self.session.scalars(
                select(ExecutionJob)
                .where(ExecutionJob.task_id == task.id)
                .order_by(ExecutionJob.created_at, ExecutionJob.id)
            )
        )

        agent_ids = {run.agent_id for run in agent_runs}
        if task.assigned_agent_id is not None:
            agent_ids.add(task.assigned_agent_id)
        agents = (
            {
                agent.id: agent
                for agent in list(
                    await self.session.scalars(select(Agent).where(Agent.id.in_(agent_ids)))
                )
            }
            if agent_ids
            else {}
        )

        dependencies = await self._task_links(task.id, upstream=True)
        dependents = await self._task_links(task.id, upstream=False)
        by_run_agent: dict[UUID, list[AgentRun]] = defaultdict(list)
        by_run_command: dict[UUID, list[DevelopmentExecution]] = defaultdict(list)
        by_run_tool: dict[UUID, list[ToolCall]] = defaultdict(list)
        by_run_verification: dict[UUID, list[AcceptanceVerification]] = defaultdict(list)
        by_iteration_review: dict[int, list[TaskReview]] = defaultdict(list)
        qa_by_run = {row.task_run_id: row for row in qa_results}
        for row in agent_runs:
            by_run_agent[row.task_run_id].append(row)
        for row in commands:
            if row.task_run_id is not None:
                by_run_command[row.task_run_id].append(row)
        for row in tool_calls:
            by_run_tool[row.task_run_id].append(row)
        for row in verifications:
            by_run_verification[row.task_run_id].append(row)
        for row in reviews:
            by_iteration_review[row.iteration].append(row)

        iterations = [
            self._iteration(
                row,
                by_run_agent[row.id],
                by_run_command[row.id],
                by_run_tool[row.id],
                qa_by_run.get(row.id),
                by_run_verification[row.id],
                by_iteration_review[row.iteration],
                agents,
            )
            for row in task_runs
        ]
        final_acceptance = self._final_acceptance(task, verifications)
        failure = self._failure(task, iterations, final_acceptance, execution_jobs)
        assigned = agents.get(task.assigned_agent_id) if task.assigned_agent_id else None
        profile = (
            await self.session.scalar(
                select(ProjectDevelopmentProfile).where(
                    ProjectDevelopmentProfile.project_id == task.project_id
                )
            )
            if task.project_id is not None
            else None
        )
        return TaskInspectionResponse(
            id=task.id,
            title=_safe_text(task.title) or task.title,
            description=_safe_text(task.description),
            kind=task.kind,
            status=task.status,
            priority=task.priority,
            iteration=task.iteration,
            max_iterations=task.max_iterations,
            acceptance_criteria=_safe_value(task.acceptance_criteria),
            assigned_agent=self._agent(assigned) if assigned else None,
            development_profile=self._profile(profile) if profile else None,
            dependencies=dependencies,
            dependents=dependents,
            iterations=iterations,
            execution_jobs=[self._execution_job(row) for row in execution_jobs],
            final_acceptance_criteria=final_acceptance,
            failure=failure,
        )

    @staticmethod
    def _execution_job(row: ExecutionJob) -> InspectionExecutionJob:
        return InspectionExecutionJob(
            id=row.id,
            status=row.status.value,
            phase=row.phase.value,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            worker_id=row.worker_id,
            started_at=row.started_at,
            completed_at=row.completed_at,
            last_error=_safe_value(row.last_error),
            failure_evidence=_safe_value(row.failure_evidence),
            phase_history=_safe_value(row.phase_history),
            retry_history=_safe_value(row.retry_history),
            environment_state=_safe_value(row.environment_state),
            checkpoint_state=_safe_value(row.checkpoint_state),
            working_tree_state=_safe_value(row.working_tree_state),
        )

    async def _task_links(self, task_id: UUID, *, upstream: bool) -> list[InspectionTaskLink]:
        linked_id = TaskDependency.depends_on_task_id if upstream else TaskDependency.task_id
        filter_column = TaskDependency.task_id if upstream else TaskDependency.depends_on_task_id
        rows = list(
            await self.session.execute(
                select(TaskDependency, Task)
                .join(Task, Task.id == linked_id)
                .where(filter_column == task_id)
                .order_by(TaskDependency.created_at, TaskDependency.id)
            )
        )
        return [
            InspectionTaskLink(
                id=linked.id,
                title=_safe_text(linked.title) or linked.title,
                status=linked.status,
                blocked_reason=await self.resolver.blocked_reason(linked.id),
            )
            for _edge, linked in rows
        ]

    def _iteration(
        self,
        task_run: TaskRun,
        agent_runs: list[AgentRun],
        commands: list[DevelopmentExecution],
        tool_calls: list[ToolCall],
        qa: QAResult | None,
        verifications: list[AcceptanceVerification],
        reviews: list[TaskReview],
        agents: dict[UUID, Agent],
    ) -> InspectionIteration:
        return InspectionIteration(
            iteration=task_run.iteration,
            task_run=InspectionTaskRun(
                id=task_run.id,
                iteration=task_run.iteration,
                status=task_run.status,
                error=_safe_value(task_run.error),
                started_at=task_run.started_at,
                completed_at=task_run.completed_at,
            ),
            agent_runs=[
                InspectionAgentRun(
                    id=row.id,
                    agent=self._agent(agents[row.agent_id]),
                    status=row.status,
                    provider=row.provider,
                    model_alias=row.model_alias,
                    model_id=row.model_id,
                    error=_error(row.error_code, row.error_message),
                    total_tokens=row.total_tokens,
                    started_at=row.started_at,
                    completed_at=row.completed_at,
                    recovery_attempts=_safe_value(row.request.get("retry_history", [])),
                )
                for row in agent_runs
            ],
            commands=[self._command(row) for row in commands],
            tool_calls=[self._tool_call(row) for row in tool_calls],
            qa_result=self._qa(qa) if qa else None,
            acceptance_criteria=[self._acceptance(row) for row in verifications],
            reviews=[
                InspectionReview(
                    id=row.id,
                    iteration=row.iteration,
                    decision=row.decision,
                    feedback=_safe_text(row.feedback),
                    created_at=row.created_at,
                )
                for row in reviews
            ],
        )

    @staticmethod
    def _agent(agent: Agent) -> InspectionAgent:
        return InspectionAgent(id=agent.id, name=agent.name, role=agent.role)

    @staticmethod
    def _command(row: DevelopmentExecution) -> InspectionCommand:
        return InspectionCommand(
            id=row.id,
            action=row.action.value,
            status=row.status,
            safe_arguments=_safe_value(row.safe_arguments),
            started_at=row.started_at,
            finished_at=row.finished_at,
            duration_ms=float(row.duration_ms) if row.duration_ms is not None else None,
            exit_code=row.exit_code,
            stdout_excerpt=_safe_text(row.stdout_excerpt),
            stderr_excerpt=_safe_text(row.stderr_excerpt),
            output_truncated=row.output_truncated,
            error=_error(row.error_code, row.error_message),
            change_summary=_safe_value(row.change_summary),
        )

    @staticmethod
    def _tool_call(row: ToolCall) -> InspectionToolCall:
        raw_error = row.error or {}
        return InspectionToolCall(
            id=row.id,
            tool_name=row.tool_name,
            status=row.status,
            safe_arguments=_safe_value(row.arguments),
            safe_result=_safe_value(row.result),
            duration_ms=row.duration_ms,
            started_at=row.started_at,
            completed_at=row.completed_at,
            error=_error(raw_error.get("code"), raw_error.get("message")),
        )

    @staticmethod
    def _qa(row: QAResult) -> InspectionQAResult:
        return InspectionQAResult(
            id=row.id,
            decision=row.decision.value,
            summary=_safe_text(row.summary) or row.summary,
            failure_classification=row.failure_classification,
            failure_code=row.failure_code,
            checks=_safe_value(row.checks),
            blocking_issues=_safe_value(row.blocking_issues),
            non_blocking_issues=_safe_value(row.non_blocking_issues),
            deterministic_checks_executed=bool(row.checks),
            created_at=row.created_at,
        )

    @staticmethod
    def _profile(row: ProjectDevelopmentProfile) -> InspectionDevelopmentProfile:
        configured = {
            action.value
            for action in (
                row.install_action,
                row.test_action,
                row.build_action,
                row.lint_action,
                row.typecheck_action,
            )
            if action is not None
        }
        node_actions = {"NODE_TEST", "NODE_BUILD", "NODE_LINT", "NODE_TYPECHECK"}
        return InspectionDevelopmentProfile(
            project_type=row.project_type.value,
            package_manager=row.package_manager.value,
            detection_source=row.detection_source,
            available_actions=sorted(configured),
            unavailable_actions=sorted(node_actions - configured),
            repository_initialized=row.repository_initialized,
            repository_branch=row.repository_branch,
            initial_checkpoint_created=row.initial_checkpoint_created,
            changed_files_count=row.changed_files_count,
            last_refreshed_at=row.last_refreshed_at,
            bootstrap_attempts=row.bootstrap_attempts,
            bootstrap_error=_error(row.bootstrap_error_code, row.bootstrap_error_message),
        )

    @staticmethod
    def _acceptance(row: AcceptanceVerification) -> InspectionAcceptance:
        return InspectionAcceptance(
            id=row.id,
            criterion_index=row.criterion_index,
            criterion=_safe_text(row.criterion) or row.criterion,
            status=row.status.value,
            verifier=row.verifier,
            evidence_summary=_safe_text(row.evidence_summary) or row.evidence_summary,
            execution_ids=_safe_value(row.execution_ids),
            created_at=row.created_at,
        )

    def _final_acceptance(
        self, task: Task, rows: list[AcceptanceVerification]
    ) -> list[InspectionAcceptance]:
        latest = {row.criterion_index: row for row in rows if row.iteration == task.iteration}
        return [
            self._acceptance(latest[index])
            if index in latest
            else InspectionAcceptance(
                criterion_index=index,
                criterion=_safe_text(str(criterion)) or str(criterion),
                status=AcceptanceVerificationStatus.UNVERIFIED.value,
                evidence_summary="Verification did not run or no durable evidence was recorded.",
                execution_ids=[],
            )
            for index, criterion in enumerate(task.acceptance_criteria)
        ]

    @staticmethod
    def _failure(
        task: Task,
        iterations: list[InspectionIteration],
        acceptance: list[InspectionAcceptance],
        execution_jobs: list[ExecutionJob],
    ) -> TaskFailureSummary | None:
        if task.status != TaskStatus.FAILED:
            return None
        final = iterations[-1] if iterations else None
        qa = final.qa_result if final else None
        failed_command = next(
            (
                row
                for row in reversed(final.commands if final else [])
                if row.status != DevelopmentExecutionStatus.SUCCEEDED
            ),
            None,
        )
        failed_tool = next(
            (
                row
                for row in reversed(final.tool_calls if final else [])
                if row.status in {ToolCallStatus.FAILED, ToolCallStatus.DENIED}
            ),
            None,
        )
        failed_agent = next(
            (row for row in reversed(final.agent_runs if final else []) if row.error is not None),
            None,
        )
        unsatisfied = [row for row in acceptance if row.status != "PASSED"]
        exhausted = bool(
            task.iteration >= task.max_iterations
            and qa is not None
            and qa.decision == QADecision.FAIL.value
        )
        failed_job = next(
            (row for row in reversed(execution_jobs) if row.status.value == "FAILED"), None
        )
        durable = failed_job.failure_evidence if failed_job is not None else None
        task_run_error = final.task_run.error if final is not None else None
        task_run_code = None
        task_run_message = None
        if isinstance(task_run_error, dict):
            raw_code = task_run_error.get("code")
            raw_message = task_run_error.get("message") or task_run_error.get("reason")
            task_run_code = raw_code if isinstance(raw_code, str) else None
            task_run_message = raw_message if isinstance(raw_message, str) else None
        elif isinstance(task_run_error, str):
            task_run_message = task_run_error
        job_error = failed_job.last_error if failed_job is not None else None
        job_code = job_error.get("code") if isinstance(job_error, dict) else None
        job_message = job_error.get("message") if isinstance(job_error, dict) else None

        failed_at = failed_job.completed_at if failed_job is not None else task.completed_at
        attempt = failed_job.attempts if failed_job is not None else None
        recovery_attempts = failed_job.retry_history if failed_job is not None else []
        evidence_source = "derived"
        agent_role = None
        if isinstance(durable, dict):
            code = str(durable.get("code") or "UNCLASSIFIED_RUNTIME_FAILURE")
            category = str(durable.get("category") or "UNCLASSIFIED_RUNTIME_FAILURE")
            message = str(
                durable.get("message")
                or "Forge recorded a failure without a classified error message."
            )
            phase = str(durable.get("phase") or failed_job.phase.value)
            raw_failed_at = durable.get("failed_at")
            if isinstance(raw_failed_at, str):
                try:
                    failed_at = datetime.fromisoformat(raw_failed_at)
                except ValueError:
                    pass
            raw_attempt = durable.get("attempt")
            attempt = raw_attempt if isinstance(raw_attempt, int) else attempt
            raw_role = durable.get("agent_role")
            agent_role = raw_role if isinstance(raw_role, str) else None
            evidence_source = "execution_job.failure_evidence"
        elif qa is not None and qa.failure_classification == "INFRASTRUCTURE_UNVERIFIABLE":
            code = qa.failure_code or "DEVELOPMENT_INFRASTRUCTURE_UNVERIFIABLE"
            category = "INFRASTRUCTURE_UNVERIFIABLE"
            message = qa.summary
            phase = "DEVELOPMENT_BOOTSTRAP"
            evidence_source = "qa_result"
        elif exhausted:
            code = "DEVELOPMENT_ITERATIONS_EXHAUSTED"
            category = "ITERATION_EXHAUSTION"
            message = (
                f"Maximum development iterations reached ({task.iteration}/{task.max_iterations}). "
                f"The Task remains failed because {len(unsatisfied)} acceptance criteria were "
                "not satisfied and deterministic QA failed."
            )
            phase = "QA"
            evidence_source = "qa_result"
        elif failed_command is not None:
            code = (
                failed_command.error.code if failed_command.error else "DEVELOPMENT_COMMAND_FAILED"
            )
            category = "COMMAND_FAILURE"
            message = failed_command.error.message if failed_command.error else "A command failed."
            phase = "DETERMINISTIC_CHECKS"
            evidence_source = "development_execution"
        elif failed_tool is not None:
            code = failed_tool.error.code if failed_tool.error else "TOOL_FAILURE"
            category = "TOOL_FAILURE"
            message = failed_tool.error.message if failed_tool.error else "A tool call failed."
            phase = "TOOL_EXECUTION"
            evidence_source = "tool_call"
        elif failed_agent is not None:
            code = failed_agent.error.code
            category = "PROVIDER_OR_MODEL_FAILURE"
            message = failed_agent.error.message
            phase = "AGENT_RUNTIME"
            evidence_source = "agent_run"
        elif task_run_code or job_code or task_run_message or job_message:
            code = str(task_run_code or job_code or "UNCLASSIFIED_RUNTIME_FAILURE")
            message = str(
                task_run_message
                or job_message
                or "Forge recorded a failure without a classified error message."
            )
            if code == "DUPLICATE_TOOL_LOOP":
                category = "AGENT_RUNTIME_GUARD"
                phase = "EXECUTING"
            elif code.startswith("MODEL_") or code == "INVALID_MODEL_OUTPUT":
                category = "PROVIDER_OR_MODEL_FAILURE"
                phase = "EXECUTING"
            elif "CHECKPOINT" in code or code.startswith("GIT_"):
                category = "CHECKPOINT_FAILURE"
                phase = "CHECKPOINTING"
            elif code.startswith("DEVELOPMENT_"):
                category = "INFRASTRUCTURE_OR_VERIFICATION_FAILURE"
                phase = "VERIFYING"
            elif "TOOL" in code:
                category = "TOOL_FAILURE"
                phase = "EXECUTING"
            else:
                category = "UNCLASSIFIED_RUNTIME_FAILURE"
                phase = failed_job.phase.value if failed_job and failed_job.phase_history else None
            evidence_source = (
                "task_run.error"
                if task_run_code or task_run_message
                else "execution_job.last_error"
            )
        else:
            code = "UNCLASSIFIED_RUNTIME_FAILURE"
            category = "UNCLASSIFIED_RUNTIME_FAILURE"
            message = "Forge recorded the Task as failed without classified runtime evidence."
            phase = None
        return TaskFailureSummary(
            category=category,
            error_code=code,
            message=message,
            failing_phase=phase,
            failing_command=failed_command.action if failed_command else None,
            command_exit_code=failed_command.exit_code if failed_command else None,
            qa_failure_reason=qa.summary if qa else None,
            failed_acceptance_criteria=unsatisfied,
            iteration_exhausted=exhausted,
            provider=failed_agent.provider if failed_agent else None,
            model=failed_agent.model_alias if failed_agent else None,
            tool_failure=failed_tool.error if failed_tool else None,
            failed_at=failed_at,
            attempt=attempt,
            agent_role=agent_role,
            recovery_attempts=_safe_value(recovery_attempts),
            evidence_source=evidence_source,
        )
