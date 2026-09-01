from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
        deterministic_executions=deterministic,
        escalations=[
            ModelEscalationResponse.model_validate(item, from_attributes=True)
            for item in escalations
        ],
    )
