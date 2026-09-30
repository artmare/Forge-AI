from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.exceptions import EntityNotFoundError
from app.domain.models import (
    AgentRun,
    DevelopmentExecution,
    Mission,
    ModelEscalation,
    ProductQAResult,
    ProjectKnowledgeIndex,
    Task,
    TaskRuntimeBudget,
    TaskRuntimeMetric,
)
from app.infrastructure.database import get_session
from app.schemas.efficient_runtime import (
    EfficiencyBenchmarkResponse,
    MissionEfficiencyResponse,
    ModelEscalationResponse,
    ProductQAResultResponse,
    ProjectKnowledgeResponse,
    RuntimeEfficiencyResponse,
)

router = APIRouter(tags=["efficient-runtime"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/tasks/{task_id}/runtime-efficiency", response_model=RuntimeEfficiencyResponse)
async def task_efficiency(task_id: UUID, session: Session) -> RuntimeEfficiencyResponse:
    if await session.get(Task, task_id) is None:
        raise EntityNotFoundError("Task")
    return await _task_efficiency(session, task_id)


@router.get("/tasks/{task_id}/efficiency-benchmark", response_model=EfficiencyBenchmarkResponse)
async def efficiency_benchmark(task_id: UUID, session: Session) -> EfficiencyBenchmarkResponse:
    efficiency = await task_efficiency(task_id, session)
    baseline = efficiency.context_bytes_sent + efficiency.estimated_unchanged_bytes_avoided
    reduction = efficiency.estimated_unchanged_bytes_avoided / baseline if baseline else 0.0
    return EfficiencyBenchmarkResponse(
        task_id=task_id,
        baseline_context_bytes=baseline,
        optimized_context_bytes=efficiency.context_bytes_sent,
        avoided_context_bytes=efficiency.estimated_unchanged_bytes_avoided,
        reduction_ratio=round(reduction, 4),
        methodology=(
            "Baseline re-sends full scoped context each turn; optimized measurement records "
            "the actual delta prompt bytes. No paid model call is used by this comparison."
        ),
    )


@router.get("/missions/{mission_id}/runtime-efficiency", response_model=MissionEfficiencyResponse)
async def mission_efficiency(mission_id: UUID, session: Session) -> MissionEfficiencyResponse:
    mission = await session.get(Mission, mission_id)
    if mission is None:
        raise EntityNotFoundError("Mission")
    task_ids = (
        list(
            await session.scalars(
                select(Task.id)
                .where(Task.project_id == mission.project_id)
                .order_by(Task.created_at)
            )
        )
        if mission.project_id is not None
        else []
    )
    tasks = [await _task_efficiency(session, task_id) for task_id in task_ids]
    sent = sum(item.context_bytes_sent for item in tasks)
    avoided = sum(item.estimated_unchanged_bytes_avoided for item in tasks)
    baseline = sent + avoided
    return MissionEfficiencyResponse(
        mission_id=mission_id,
        task_count=len(tasks),
        model_calls=sum(item.model_calls for item in tasks),
        input_tokens=sum(item.input_tokens for item in tasks),
        output_tokens=sum(item.output_tokens for item in tasks),
        cached_tokens=sum(item.cached_tokens for item in tasks),
        estimated_cost=sum((item.estimated_cost for item in tasks), start=0),
        context_bytes_sent=sent,
        estimated_unchanged_bytes_avoided=avoided,
        context_reduction_ratio=round(avoided / baseline, 4) if baseline else 0.0,
        escalations=sum(len(item.escalations) for item in tasks),
        tasks=tasks,
    )


@router.get("/tasks/{task_id}/product-qa-results", response_model=list[ProductQAResultResponse])
async def product_qa_results(
    task_id: UUID,
    session: Session,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[ProductQAResultResponse]:
    if await session.get(Task, task_id) is None:
        raise EntityNotFoundError("Task")
    results = list(
        await session.scalars(
            select(ProductQAResult)
            .where(ProductQAResult.task_id == task_id)
            .order_by(ProductQAResult.iteration, ProductQAResult.created_at)
            .offset(offset)
            .limit(limit)
        )
    )
    return [ProductQAResultResponse.model_validate(item) for item in results]


@router.get("/projects/{project_id}/knowledge-index", response_model=ProjectKnowledgeResponse)
async def knowledge_index(project_id: UUID, session: Session) -> ProjectKnowledgeResponse:
    index = await session.scalar(
        select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == project_id)
    )
    if index is None:
        raise EntityNotFoundError("ProjectKnowledgeIndex")
    return ProjectKnowledgeResponse.model_validate(index)


async def _task_efficiency(session: AsyncSession, task_id: UUID) -> RuntimeEfficiencyResponse:
    totals = (
        await session.execute(
            select(
                func.count(AgentRun.id),
                func.coalesce(func.sum(AgentRun.input_tokens), 0),
                func.coalesce(func.sum(AgentRun.output_tokens), 0),
                func.coalesce(func.sum(AgentRun.cached_tokens), 0),
                func.coalesce(func.sum(AgentRun.estimated_cost), 0),
            ).where(
                AgentRun.task_id == task_id,
                # Deterministic QA runs do not consume model-call/token budget.
                AgentRun.provider != "deterministic",
            )
        )
    ).one()
    budget = await session.scalar(
        select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == task_id)
    )
    metric = await session.scalar(
        select(TaskRuntimeMetric).where(TaskRuntimeMetric.task_id == task_id)
    )
    escalations = list(
        await session.scalars(
            select(ModelEscalation)
            .where(ModelEscalation.task_id == task_id)
            .order_by(ModelEscalation.created_at)
        )
    )
    deterministic = int(
        await session.scalar(
            select(func.count(DevelopmentExecution.id)).where(
                DevelopmentExecution.task_id == task_id
            )
        )
        or 0
    )
    sent = metric.context_bytes_sent if metric else 0
    avoided = metric.estimated_unchanged_bytes_avoided if metric else 0
    baseline = sent + avoided
    return RuntimeEfficiencyResponse(
        task_id=task_id,
        model_calls=int(totals[0]),
        input_tokens=int(totals[1]),
        output_tokens=int(totals[2]),
        cached_tokens=int(totals[3]),
        estimated_cost=totals[4],
        current_model_alias=budget.current_model_alias if budget else None,
        budget_warning=budget.warning_active if budget else False,
        stopped_reason=budget.stopped_reason if budget else None,
        limits={
            "model_calls": budget.max_model_calls if budget else None,
            "calls_per_iteration": budget.max_calls_per_iteration if budget else None,
            "input_tokens": budget.max_input_tokens if budget else None,
            "output_tokens": budget.max_output_tokens if budget else None,
            "estimated_cost": budget.max_estimated_cost if budget else None,
        },
        context_bytes_sent=sent,
        estimated_unchanged_bytes_avoided=avoided,
        context_reduction_ratio=round(avoided / baseline, 4) if baseline else 0.0,
        repeated_reads_avoided=metric.repeated_reads_avoided if metric else 0,
        duplicate_turns_detected=metric.duplicate_turns_detected if metric else 0,
        reused_observations=metric.reused_observations if metric else 0,
        stale_observation_invalidations=(metric.stale_observation_invalidations if metric else 0),
        malformed_tool_repairs=metric.malformed_tool_repairs if metric else 0,
        stagnation_signals=metric.stagnation_signals if metric else 0,
        context_component_bytes=metric.context_component_bytes if metric else {},
        last_useful_action=metric.last_useful_action if metric else {},
        deterministic_executions=deterministic,
        escalations=[
            ModelEscalationResponse.model_validate(item, from_attributes=True)
            for item in escalations
        ],
    )


@router.get("/tasks/{task_id}/dev-mode")
async def dev_mode_status(task_id: UUID, session: Session) -> dict:
    """Existing task inspection boundary; contains only Forge-owned durable event data."""
    from app.domain.models import (
        Event,
        ModelCallRecord,
        TaskRuntimeBudget,
        TaskRuntimeMetric,
        ToolCall,
    )

    task = await session.get(Task, task_id)
    if task is None:
        raise EntityNotFoundError("Task")
    events = list(
        await session.scalars(
            select(Event)
            .where(Event.task_id == task_id, Event.type.like("DEV_%"))
            .order_by(Event.created_at.desc())
            .limit(100)
        )
    )
    calls = list(
        await session.scalars(
            select(ModelCallRecord)
            .where(ModelCallRecord.task_id == task_id)
            .order_by(ModelCallRecord.created_at.desc())
            .limit(100)
        )
    )
    tools = list(
        await session.scalars(
            select(ToolCall)
            .where(ToolCall.task_id == task_id)
            .order_by(ToolCall.created_at.desc())
            .limit(100)
        )
    )
    budget = await session.scalar(
        select(TaskRuntimeBudget).where(TaskRuntimeBudget.task_id == task_id)
    )
    metric = await session.scalar(
        select(TaskRuntimeMetric).where(TaskRuntimeMetric.task_id == task_id)
    )
    latest_rollover = next((e for e in events if e.type == "DEV_CONTEXT_ROLLOVER"), None)
    return {
        "task_id": str(task_id),
        "status": task.status.value,
        "events": [
            {"type": e.type, "at": e.created_at.isoformat(), "details": e.details} for e in events
        ],
        "models": [
            {"provider": c.provider, "model": c.model_id, "status": c.status} for c in calls
        ],
        "tools": [{"id": str(c.id), "name": c.tool_name, "status": c.status.value} for c in tools],
        "rollover_count": sum(e.type == "DEV_CONTEXT_ROLLOVER" for e in events),
        "rollover_limit": get_settings().forge_dev_max_rollovers,
        "rollover_reasons": [
            {
                "count": event.details.get("rollover_count"),
                "reason": event.details.get("reason"),
                "estimated_input_tokens": event.details.get("estimated_input_tokens"),
                "progress": event.details.get("progress_since_last_rollover", []),
            }
            for event in reversed(events)
            if event.type == "DEV_CONTEXT_ROLLOVER"
        ],
        "specialist_calls": sum(e.type == "DEV_SPECIALIST_RESERVED" for e in events),
        "human_promotion_required": True,
        "context_budget": (
            {
                "input_tokens_consumed": budget.consumed_input_tokens,
                "cached_input_tokens": budget.consumed_cached_tokens,
                "input_token_budget": budget.max_input_tokens,
                "remaining_input_tokens": max(
                    budget.max_input_tokens - budget.consumed_input_tokens, 0
                ),
                "model_calls": budget.consumed_model_calls,
                "context_bytes_sent": metric.context_bytes_sent if metric else 0,
                "context_component_bytes": (metric.context_component_bytes if metric else {}),
                "cached_observations_reused": (metric.reused_observations if metric else 0),
                "stale_cache_invalidations": (
                    metric.stale_observation_invalidations if metric else 0
                ),
                "malformed_tool_repair_attempts": (metric.malformed_tool_repairs if metric else 0),
                "stagnation_signals": metric.stagnation_signals if metric else 0,
                "last_useful_action": metric.last_useful_action if metric else {},
                "last_rollover": latest_rollover.details if latest_rollover else None,
            }
            if budget
            else None
        ),
    }


@router.get("/tasks/{task_id}/browser-artifacts/{artifact_id}")
async def browser_artifact(task_id: UUID, artifact_id: UUID, session: Session):
    from pathlib import Path

    from fastapi.responses import FileResponse

    from app.core.config import get_settings
    from app.domain.enums import ToolCallStatus
    from app.domain.models import ToolCall

    reference = f"browser/{artifact_id}.png"
    call = await session.scalar(
        select(ToolCall)
        .where(
            ToolCall.task_id == task_id,
            ToolCall.tool_name == "browser.capture",
            ToolCall.status == ToolCallStatus.SUCCEEDED,
            ToolCall.result["artifact"].astext == reference,
        )
        .limit(1)
    )
    if call is None:
        raise EntityNotFoundError("Browser artifact")
    path = (
        Path(get_settings().development_runner_queue_root)
        / "browser/artifacts"
        / f"{artifact_id}.png"
    )
    if path.is_symlink() or not path.is_file():
        raise EntityNotFoundError("Browser artifact")
    return FileResponse(path, media_type="image/png", filename=f"{artifact_id}.png")
