from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.context_rollover import mutation_key
from app.core.config import Settings
from app.domain.enums import TaskKind, TaskStatus
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import (
    AgentRun,
    Event,
    FileContextCache,
    ModelEscalation,
    Task,
    TaskRun,
    TaskRuntimeBudget,
    TaskRuntimeMetric,
    ToolCall,
)
from app.services.event_factory import EventFactory
from app.tool_system.contracts import ToolObservation, ToolRequestTurn


@dataclass(frozen=True)
class BudgetSnapshot:
    calls: int
    iteration_calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost: Decimal


class ModelBudgetExceeded(RuntimeError):
    def __init__(
        self, reason: str, budget: TaskRuntimeBudget, snapshot: BudgetSnapshot
    ) -> None:
        self.reason = reason
        self.budget = budget
        self.snapshot = snapshot
        super().__init__(reason)


class EfficientRuntimeService:
    """Durable budget, routing, and duplicate-context accounting for AgentRuntime."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def enforce_budget(self, task: Task) -> TaskRuntimeBudget:
        budget = await self._budget(task.id)
        iteration = task.iteration if task.status == TaskStatus.IN_PROGRESS else task.iteration + 1
        snapshot = await self.snapshot(task.id, iteration)
        budget.consumed_model_calls = snapshot.calls
        budget.consumed_input_tokens = snapshot.input_tokens
        budget.consumed_output_tokens = snapshot.output_tokens
        budget.consumed_cached_tokens = snapshot.cached_tokens
        budget.consumed_estimated_cost = snapshot.estimated_cost
        reason = self._exhausted_reason(budget, snapshot)
        if reason:
            budget.stopped_reason = reason
            await self.session.flush()
            raise ModelBudgetExceeded(reason, budget, snapshot)
        budget.warning_active = self._near_limit(budget, snapshot)
        budget.stopped_reason = None
        await self.session.flush()
        return budget

    async def approve_input_budget_resume(
        self, task_id: UUID, additional_input_tokens: int
    ) -> Task:
        """Explicit human recovery; preserves consumption and durable task evidence."""
        task = await self.session.scalar(
            select(Task).where(Task.id == task_id).with_for_update()
        )
        budget = await self.session.scalar(
            select(TaskRuntimeBudget)
            .where(TaskRuntimeBudget.task_id == task_id)
            .with_for_update()
        )
        if (
            task is None
            or budget is None
            or task.status != TaskStatus.FAILED
            or budget.stopped_reason
            not in {
                "MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED",
                "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED",
            }
        ):
            raise AgentRuntimeDomainError(
                "TASK_BUDGET_RESUME_NOT_ALLOWED",
                "Task is not eligible for input-token budget resume",
                409,
            )
        await self._ensure_resume_handoff(task, budget)
        previous = budget.max_input_tokens
        budget.max_input_tokens += additional_input_tokens
        budget.stopped_reason = None
        budget.warning_active = False
        task.status = TaskStatus.QUEUED
        task.completed_at = None
        task.terminal_reason = None
        task.max_iterations = max(task.max_iterations, task.iteration + 1)
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_BUDGET_RESUME_APPROVED",
            message="Human approved bounded continuation from the recovery handoff.",
            payload={
                "previous_input_token_budget": previous,
                "additional_input_tokens": additional_input_tokens,
                "new_input_token_budget": budget.max_input_tokens,
                "consumed_input_tokens": budget.consumed_input_tokens,
            },
        )
        await self.session.commit()
        return task

    async def _ensure_resume_handoff(
        self, task: Task, budget: TaskRuntimeBudget
    ) -> None:
        existing = await self.session.scalar(
            select(Event.id)
            .where(Event.task_id == task.id, Event.type == "DEV_BUDGET_HANDOFF")
            .limit(1)
        )
        if existing is not None:
            return
        checkpoint: dict[str, object] = {"created": False, "reason": "not development"}
        if task.kind == TaskKind.DEVELOPMENT:
            from app.development.bootstrap import ProjectBootstrapService

            checkpoint = await ProjectBootstrapService(
                self.session, settings=self.settings
            ).checkpoint_recovery(task.id, budget.stopped_reason or "INPUT_TOKEN_BUDGET")
            if checkpoint.get("reason"):
                raise AgentRuntimeDomainError(
                    "TASK_BUDGET_RECOVERY_CHECKPOINT_FAILED",
                    "Development work could not be checkpointed for safe budget resume",
                    409,
                )
            await self.session.refresh(task, with_for_update=True)
            await self.session.refresh(budget, with_for_update=True)
            if task.status != TaskStatus.FAILED or budget.stopped_reason not in {
                "MODEL_INPUT_TOKEN_BUDGET_EXHAUSTED",
                "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED",
            }:
                raise AgentRuntimeDomainError(
                    "TASK_BUDGET_RESUME_NOT_ALLOWED",
                    "Task changed while the recovery checkpoint was created",
                    409,
                )
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == task.id)
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        successful = [call for call in calls if call.status.value == "SUCCEEDED"]
        fingerprints = sorted(
            key
            for call in successful
            if (key := mutation_key(call.tool_name, dict(call.arguments))) is not None
        )
        files = sorted(
            {
                str(call.result["path"])
                for call in successful
                if call.tool_name in {"filesystem.write", "filesystem.patch"}
                and call.result
                and call.result.get("path")
            }
        )
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_BUDGET_HANDOFF",
            message="Legacy budget stop reconstructed from authoritative Forge evidence.",
            payload={
                "task_id": str(task.id),
                "goal": task.title[:2000],
                "phase": "BUDGET_STOP",
                "stop_reason": budget.stopped_reason,
                "legacy_reconstructed": True,
                "completed": [
                    {
                        "tool": call.tool_name,
                        "status": call.status.value,
                        "reference": (call.result or {}).get("path")
                        or (call.result or {}).get("action"),
                    }
                    for call in calls[-30:]
                ],
                "files_modified": files,
                "tool_steps": len(calls),
                "rollover_count": 0,
                "duplicate_signals": 0,
                "mutation_fingerprints": fingerprints,
                "input_tokens_consumed": budget.consumed_input_tokens,
                "cached_input_tokens_consumed": budget.consumed_cached_tokens,
                "input_token_budget": budget.max_input_tokens,
                "remaining_input_tokens": max(
                    budget.max_input_tokens - budget.consumed_input_tokens, 0
                ),
                "checkpoint": checkpoint,
                "next_action": (
                    "Continue from reconstructed durable evidence; do not replay mutations."
                ),
            },
        )

    async def snapshot(self, task_id: UUID, iteration: int) -> BudgetSnapshot:
        totals = (
            await self.session.execute(
                select(
                    func.count(AgentRun.id),
                    func.coalesce(func.sum(AgentRun.input_tokens), 0),
                    func.coalesce(func.sum(AgentRun.output_tokens), 0),
                    func.coalesce(func.sum(AgentRun.cached_tokens), 0),
                    func.coalesce(func.sum(AgentRun.estimated_cost), 0),
                ).where(
                    AgentRun.task_id == task_id,
                    # Deterministic QA AgentRuns are audit records, not provider/model calls.
                    AgentRun.provider != "deterministic",
                )
            )
        ).one()
        iteration_calls = int(
            await self.session.scalar(
                select(func.count(AgentRun.id))
                .join(TaskRun, TaskRun.id == AgentRun.task_run_id)
                .where(
                    AgentRun.task_id == task_id,
                    AgentRun.provider != "deterministic",
                    TaskRun.iteration == iteration,
                )
            )
            or 0
        )
        return BudgetSnapshot(
            calls=int(totals[0]),
            iteration_calls=iteration_calls,
            input_tokens=int(totals[1]),
            output_tokens=int(totals[2]),
            cached_tokens=int(totals[3]),
            estimated_cost=Decimal(str(totals[4])),
        )

    async def route(
        self,
        task: Task,
        role: str,
        requested_alias: str,
        duplicate_signals: int,
    ) -> str:
        normalized_role = role.strip().upper()
        if normalized_role == "DEVELOPER":
            alias = "developer_coding"
        elif normalized_role == "QA":
            alias = "qa_fast"
        else:
            alias = requested_alias
        signals: list[str] = []
        if duplicate_signals > 0:
            signals.append("REPEATED_UNCHANGED_TOOL_ACTION")
        if task.iteration >= 2:
            signals.append("MULTIPLE_FAILED_ITERATIONS")
        target = alias
        if normalized_role == "DEVELOPER" and signals:
            target = "developer_reasoning"
        if target != alias:
            exists = await self.session.scalar(
                select(ModelEscalation.id).where(
                    ModelEscalation.task_id == task.id,
                    ModelEscalation.to_alias == target,
                    ModelEscalation.reason_code == "OBJECTIVE_COMPLEXITY_SIGNAL",
                )
            )
            if exists is None:
                task_run = await self.session.scalar(
                    select(TaskRun)
                    .where(TaskRun.task_id == task.id)
                    .order_by(TaskRun.iteration.desc())
                    .limit(1)
                )
                self.session.add(
                    ModelEscalation(
                        task_id=task.id,
                        task_run_id=task_run.id if task_run else None,
                        from_alias=alias,
                        to_alias=target,
                        reason_code="OBJECTIVE_COMPLEXITY_SIGNAL",
                        reason=(
                            "A stronger configured model was selected from persisted "
                            "runtime signals."
                        ),
                        objective_signals=signals,
                    )
                )
        budget = await self._budget(task.id)
        budget.current_model_alias = target
        await self.session.flush()
        return target

    async def record_context(self, task_id: UUID, sent_bytes: int, avoided_bytes: int) -> None:
        metric = await self._metric(task_id)
        metric.context_bytes_sent += sent_bytes
        metric.estimated_unchanged_bytes_avoided += max(avoided_bytes, 0)
        await self.session.flush()

    async def record_tool_request(self, task_id: UUID, turn: ToolRequestTurn) -> int:
        metric = await self._metric(task_id)
        signature = hashlib.sha256(
            json.dumps(
                {"tool": turn.tool_name, "arguments": turn.arguments},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        counts = dict(metric.tool_signature_counts)
        # A loop is consecutive repetition of the same unchanged request. Historical
        # uses of the same read are legitimate after another tool (especially a write)
        # has changed workspace state, so they must not accumulate forever.
        previous = counts.get("__last_signature__")
        count = int(counts.get("__last_count__", 0)) + 1 if previous == signature else 1
        counts[signature] = count
        counts["__last_signature__"] = signature
        counts["__last_count__"] = count
        metric.tool_signature_counts = counts
        if count > 1:
            metric.duplicate_turns_detected += 1
            if turn.tool_name == "filesystem.read":
                metric.repeated_reads_avoided += 1
        await self.session.flush()
        return count - 1

    async def cache_observation(
        self, task: Task, turn: ToolRequestTurn, observation: ToolObservation
    ) -> None:
        if task.project_id is None or turn.tool_name not in {"filesystem.read", "filesystem.write"}:
            return
        path = turn.arguments.get("path")
        if not isinstance(path, str):
            return
        content = None
        if turn.tool_name == "filesystem.read" and observation.result:
            value = observation.result.get("content")
            content = value if isinstance(value, str) else None
        elif turn.tool_name == "filesystem.write":
            value = turn.arguments.get("content")
            content = value if isinstance(value, str) else None
        if content is None:
            return
        digest = hashlib.sha256(content.encode()).hexdigest()
        cache = await self.session.scalar(
            select(FileContextCache).where(
                FileContextCache.project_id == task.project_id,
                FileContextCache.path == path,
            )
        )
        summary = self.safe_file_summary(path, content)
        if cache is None:
            cache = FileContextCache(
                project_id=task.project_id,
                path=path,
                content_hash=digest,
                last_model_visible_hash=digest,
                safe_summary=summary,
                byte_size=len(content.encode()),
            )
            self.session.add(cache)
        else:
            cache.content_hash = digest
            cache.last_model_visible_hash = digest
            cache.safe_summary = summary
            cache.byte_size = len(content.encode())
        await self.session.flush()

    @staticmethod
    def safe_file_summary(path: str, content: str) -> str:
        lines = content.count("\n") + 1
        return f"{path}: {lines} lines, {len(content.encode())} bytes, sha256 recorded"

    async def _budget(self, task_id: UUID) -> TaskRuntimeBudget:
        budget = await self.session.scalar(
            select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == task_id)
        )
        if budget is None:
            budget = TaskRuntimeBudget(
                task_id=task_id,
                max_model_calls=self.settings.max_model_calls_per_task,
                max_calls_per_iteration=self.settings.max_model_calls_per_iteration,
                max_input_tokens=self.settings.max_input_tokens_per_task,
                max_output_tokens=self.settings.max_output_tokens_per_task,
                max_free_model_calls=self.settings.max_free_model_calls_per_task,
                max_paid_model_calls=self.settings.max_paid_model_calls_per_task,
                max_estimated_cost=(
                    Decimal(
                        str(
                            self.settings.max_ai_spend_per_task
                            if self.settings.max_ai_spend_per_task is not None
                            else self.settings.max_estimated_cost_per_task
                        )
                    )
                    if (
                        self.settings.max_ai_spend_per_task is not None
                        or self.settings.max_estimated_cost_per_task is not None
                    )
                    else None
                ),
            )
            self.session.add(budget)
            await self.session.flush()
        return budget

    async def _metric(self, task_id: UUID) -> TaskRuntimeMetric:
        metric = await self.session.scalar(
            select(TaskRuntimeMetric).where(TaskRuntimeMetric.task_id == task_id)
        )
        if metric is None:
            metric = TaskRuntimeMetric(task_id=task_id)
            self.session.add(metric)
            await self.session.flush()
        return metric

    @staticmethod
    def _exhausted_reason(budget: TaskRuntimeBudget, snapshot: BudgetSnapshot) -> str | None:
        if snapshot.calls >= budget.max_model_calls:
            return "MODEL_CALL_BUDGET_EXHAUSTED"
        if snapshot.iteration_calls >= budget.max_calls_per_iteration:
            return "MODEL_ITERATION_CALL_BUDGET_EXHAUSTED"
        if snapshot.input_tokens >= budget.max_input_tokens:
            return "TASK_INPUT_TOKEN_BUDGET_EXHAUSTED"
        if snapshot.output_tokens >= budget.max_output_tokens:
            return "MODEL_OUTPUT_TOKEN_BUDGET_EXHAUSTED"
        if (
            budget.max_estimated_cost is not None
            and snapshot.estimated_cost >= budget.max_estimated_cost
        ):
            return "MODEL_COST_BUDGET_EXHAUSTED"
        return None

    def _near_limit(self, budget: TaskRuntimeBudget, snapshot: BudgetSnapshot) -> bool:
        ratio = self.settings.model_budget_warning_ratio
        return any(
            (
                snapshot.calls / budget.max_model_calls,
                snapshot.iteration_calls / budget.max_calls_per_iteration,
                snapshot.input_tokens / budget.max_input_tokens,
                snapshot.output_tokens / budget.max_output_tokens,
            )[index]
            >= ratio
            for index in range(4)
        )
