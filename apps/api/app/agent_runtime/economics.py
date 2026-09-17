from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.contracts import ModelResponse, ModelUsage, ProviderCallError
from app.agent_runtime.cost import CostEstimator
from app.agent_runtime.routing import (
    ModelProfile,
    ModelSelection,
    ProviderHealthStatus,
)
from app.core.config import Settings
from app.domain.models import (
    Company,
    Mission,
    ModelCallRecord,
    ModelProviderHealth,
    Task,
    TaskRuntimeBudget,
)


@dataclass(frozen=True)
class EconomicSnapshot:
    paid_budget_remaining: Decimal
    task_calls: int
    mission_calls: int
    free_task_calls: int
    paid_task_calls: int


class ModelEconomicsService:
    """Durable pre-call reservations, limits, health, and usage accounting."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.costs = CostEstimator(settings.model_pricing)

    async def mission_for_task(self, task: Task) -> Mission | None:
        if task.project_id is None:
            return None
        return await self.session.scalar(
            select(Mission).where(Mission.project_id == task.project_id)
        )

    async def snapshot(
        self,
        *,
        task: Task | None = None,
        mission: Mission | None = None,
    ) -> EconomicSnapshot:
        if mission is None and task is not None:
            mission = await self.mission_for_task(task)
        company_id = (
            task.company_id if task is not None else mission.company_id if mission else None
        )
        company = await self.session.get(Company, company_id) if company_id else None

        async def pending_for(column: object, identifier: UUID) -> Decimal:
            value = await self.session.scalar(
                select(func.coalesce(func.sum(ModelCallRecord.reserved_cost), 0)).where(
                    ModelCallRecord.status == "STARTED",
                    ModelCallRecord.paid.is_(True),
                    column == identifier,
                )
            )
            return Decimal(str(value or 0))

        remaining: list[Decimal] = []
        if company is not None and company.paid_ai_budget > 0:
            remaining.append(
                company.paid_ai_budget
                - company.ai_spend_recorded
                - await pending_for(ModelCallRecord.company_id, company.id)
            )
        if mission is not None:
            remaining.append(
                mission.paid_ai_budget
                - mission.ai_spend_recorded
                - await pending_for(ModelCallRecord.mission_id, mission.id)
            )
            if self.settings.max_ai_spend_per_mission is not None:
                remaining.append(
                    Decimal(str(self.settings.max_ai_spend_per_mission))
                    - mission.ai_spend_recorded
                    - await pending_for(ModelCallRecord.mission_id, mission.id)
                )
        if task is not None and task.paid_ai_budget is not None:
            remaining.append(
                task.paid_ai_budget
                - task.ai_spend_recorded
                - await pending_for(ModelCallRecord.task_id, task.id)
            )
        if task is not None and self.settings.max_ai_spend_per_task is not None:
            remaining.append(
                Decimal(str(self.settings.max_ai_spend_per_task))
                - task.ai_spend_recorded
                - await pending_for(ModelCallRecord.task_id, task.id)
            )
        paid_remaining = max(min(remaining), Decimal("0")) if remaining else Decimal("0")
        if not self.settings.allow_paid_model_calls:
            paid_remaining = Decimal("0")

        task_calls = free_calls = paid_calls = 0
        if task is not None:
            task_calls, free_calls, paid_calls = (
                await self.session.execute(
                    select(
                        func.count(ModelCallRecord.id),
                        func.count(ModelCallRecord.id).filter(ModelCallRecord.paid.is_(False)),
                        func.count(ModelCallRecord.id).filter(ModelCallRecord.paid.is_(True)),
                    ).where(ModelCallRecord.task_id == task.id)
                )
            ).one()
        mission_calls = 0
        if mission is not None:
            mission_calls = int(
                await self.session.scalar(
                    select(func.count(ModelCallRecord.id)).where(
                        ModelCallRecord.mission_id == mission.id
                    )
                )
                or 0
            )
        return EconomicSnapshot(
            paid_budget_remaining=paid_remaining,
            task_calls=int(task_calls),
            mission_calls=mission_calls,
            free_task_calls=int(free_calls),
            paid_task_calls=int(paid_calls),
        )

    async def provider_health(
        self, profiles: tuple[ModelProfile, ...]
    ) -> dict[tuple[str, str], ProviderHealthStatus]:
        rows = list(await self.session.scalars(select(ModelProviderHealth)))
        now = datetime.now(UTC)
        cooldown = timedelta(seconds=self.settings.model_provider_health_cooldown_seconds)
        health: dict[tuple[str, str], ProviderHealthStatus] = {}
        for row in rows:
            status = ProviderHealthStatus(row.status)
            if (
                status
                in {
                    ProviderHealthStatus.QUOTA_EXHAUSTED,
                    ProviderHealthStatus.DEGRADED,
                }
                and row.last_checked_at <= now - cooldown
            ):
                # Quota and transient degradation are observations, not permanent disables.
                # A post-cooldown request rechecks the provider; auth/model errors remain
                # fail-closed until configuration or health state is explicitly repaired.
                status = ProviderHealthStatus.HEALTHY
            health[(row.provider, row.model_id)] = status
        for profile in profiles:
            if not self._provider_enabled(profile.provider) or not self._credential_present(
                profile.provider
            ):
                health[(profile.provider, profile.model_id)] = ProviderHealthStatus.AUTH_FAILED
        return health

    async def begin_call(
        self,
        selection: ModelSelection,
        *,
        capabilities: frozenset[str],
        agent_role: str,
        task: Task | None = None,
        mission: Mission | None = None,
        agent_run_id: UUID | None = None,
        planning_run_id: UUID | None = None,
        fallback_from: tuple[str, str] | None = None,
        fallback_reason: str | None = None,
    ) -> ModelCallRecord:
        if mission is None and task is not None:
            mission = await self.mission_for_task(task)
        # Serialize reservations against the durable budget owners. The locks are
        # released before the provider request, never held across network work.
        company_id = (
            task.company_id if task is not None else mission.company_id if mission else None
        )
        if company_id is not None:
            await self.session.scalar(
                select(Company)
                .where(Company.id == company_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if mission is not None:
            mission = await self.session.scalar(
                select(Mission)
                .where(Mission.id == mission.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if task is not None:
            task = await self.session.scalar(
                select(Task)
                .where(Task.id == task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task is None:
                raise ProviderCallError(
                    "MODEL_ACCOUNTING_STATE_LOST",
                    "Task model-call accounting state was not available",
                    category="INFRASTRUCTURE",
                )
        snapshot = await self.snapshot(task=task, mission=mission)
        if snapshot.mission_calls >= self.settings.max_model_calls_per_mission:
            raise ProviderCallError(
                "MISSION_MODEL_CALL_LIMIT_EXHAUSTED",
                "Mission model-call limit reached",
                category="BUDGET",
            )
        if task is not None:
            budget = await self._task_budget(task.id)
            if snapshot.task_calls >= budget.max_model_calls:
                raise ProviderCallError(
                    "MODEL_CALL_BUDGET_EXHAUSTED",
                    "Task model-call limit reached",
                    category="BUDGET",
                )
            if selection.profile.paid and snapshot.paid_task_calls >= budget.max_paid_model_calls:
                raise ProviderCallError(
                    "PAID_MODEL_CALL_LIMIT_EXHAUSTED",
                    "Task paid-model call limit reached",
                    category="BUDGET",
                )
            if (
                not selection.profile.paid
                and snapshot.free_task_calls >= budget.max_free_model_calls
            ):
                raise ProviderCallError(
                    "FREE_MODEL_CALL_LIMIT_EXHAUSTED",
                    "Task free-model call limit reached",
                    category="BUDGET",
                )
        reserve = Decimal(str(selection.profile.max_call_cost or 0))
        if selection.profile.paid and (
            selection.profile.max_call_cost is None or reserve > snapshot.paid_budget_remaining
        ):
            raise ProviderCallError(
                "MODEL_BUDGET_UNAVAILABLE",
                "Paid model call is not covered by the available AI budget",
                category="BUDGET",
            )
        record = ModelCallRecord(
            company_id=task.company_id if task else mission.company_id if mission else None,
            mission_id=mission.id if mission else None,
            task_id=task.id if task else None,
            agent_run_id=agent_run_id,
            planning_run_id=planning_run_id,
            provider=selection.profile.provider,
            model_id=selection.profile.model_id,
            economic_tier=selection.profile.tier.value,
            paid=selection.profile.paid,
            status="STARTED",
            agent_role=agent_role,
            selection_reason=selection.reason,
            required_capabilities=sorted(capabilities),
            fallback_from_provider=fallback_from[0] if fallback_from else None,
            fallback_from_model=fallback_from[1] if fallback_from else None,
            fallback_reason=fallback_reason,
            reserved_cost=reserve,
            estimated_cost=Decimal("0"),
            started_at=datetime.now(UTC),
        )
        self.session.add(record)
        await self.session.commit()
        return record

    async def complete_call(
        self,
        record_id: UUID,
        *,
        response: ModelResponse | None = None,
        error: ProviderCallError | None = None,
    ) -> ModelCallRecord:
        record = await self.session.scalar(
            select(ModelCallRecord).where(ModelCallRecord.id == record_id).with_for_update()
        )
        if record is None or record.status != "STARTED":
            raise ProviderCallError(
                "MODEL_ACCOUNTING_STATE_LOST",
                "Model-call accounting state was not available",
                category="INFRASTRUCTURE",
            )
        now = datetime.now(UTC)
        record.completed_at = now
        if error is not None:
            record.status = "FAILED"
            record.error_code = error.code
            await self._update_health(record.provider, record.model_id, error)
        else:
            usage = response.usage if response else ModelUsage()
            estimated = (
                Decimal("0")
                if not record.paid
                else self.costs.estimate(record.provider, record.model_id, usage)
            )
            if estimated is None:
                estimated = record.reserved_cost
            record.status = "SUCCEEDED"
            record.input_tokens = usage.input_tokens
            record.output_tokens = usage.output_tokens
            record.cached_tokens = usage.cached_input_tokens
            record.estimated_cost = estimated
            await self._record_spend(record, estimated)
            await self._update_health(record.provider, record.model_id, None)
        record.reserved_cost = Decimal("0")
        await self.session.commit()
        return record

    async def _record_spend(self, record: ModelCallRecord, amount: Decimal) -> None:
        if amount <= 0:
            return
        if record.company_id:
            company = await self.session.get(Company, record.company_id)
            if company:
                company.ai_spend_recorded += amount
        if record.mission_id:
            mission = await self.session.get(Mission, record.mission_id)
            if mission:
                mission.ai_spend_recorded += amount
        if record.task_id:
            task = await self.session.get(Task, record.task_id)
            if task:
                task.ai_spend_recorded += amount

    async def _update_health(
        self, provider: str, model_id: str, error: ProviderCallError | None
    ) -> None:
        health = await self.session.scalar(
            select(ModelProviderHealth).where(
                ModelProviderHealth.provider == provider,
                ModelProviderHealth.model_id == model_id,
            )
        )
        if health is None:
            health = ModelProviderHealth(provider=provider, model_id=model_id)
            self.session.add(health)
        if error is None:
            health.status = ProviderHealthStatus.HEALTHY.value
            health.failure_code = None
        else:
            health.status = {
                "MODEL_QUOTA_EXHAUSTED": ProviderHealthStatus.QUOTA_EXHAUSTED.value,
                "MODEL_AUTH_ERROR": ProviderHealthStatus.AUTH_FAILED.value,
                "MODEL_UNAVAILABLE": ProviderHealthStatus.MODEL_UNAVAILABLE.value,
            }.get(error.code, ProviderHealthStatus.DEGRADED.value)
            health.failure_code = error.code
        health.last_checked_at = datetime.now(UTC)

    async def _task_budget(self, task_id: UUID) -> TaskRuntimeBudget:
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

    def _provider_enabled(self, provider: str) -> bool:
        return {
            "mock": True,
            "openai": self.settings.openai_enabled,
            "gemini": self.settings.gemini_enabled,
            "openrouter": self.settings.openrouter_enabled,
        }.get(provider, False)

    def _credential_present(self, provider: str) -> bool:
        if provider == "mock":
            return True
        secret = {
            "openai": self.settings.openai_api_key,
            "gemini": self.settings.gemini_api_key,
            "openrouter": self.settings.openrouter_api_key,
        }.get(provider)
        return bool(secret and secret.get_secret_value().strip())
