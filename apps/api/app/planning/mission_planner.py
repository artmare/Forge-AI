import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.contracts import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderCallError,
)
from app.agent_runtime.cost import CostEstimator
from app.agent_runtime.economics import ModelEconomicsService
from app.agent_runtime.policy import ModelExecutionPolicy
from app.agent_runtime.providers import provider_for_profile
from app.agent_runtime.registry import ModelRegistry
from app.agent_runtime.routing import (
    EconomicTier,
    ModelCapability,
    ModelProfile,
    ModelRouter,
    ModelSelection,
    RoutingContext,
    required_capabilities,
)
from app.core.config import Settings, get_settings
from app.domain.enums import MissionStatus, PlanningRunStatus
from app.domain.exceptions import (
    EntityNotFoundError,
    MissionConflictError,
    MissionPlanningError,
)
from app.domain.models import Mission, PlanningRun
from app.planning.contracts import PlanProposal, PlanValidationError, PlanValidationResult
from app.planning.validator import PlanValidator
from app.repositories.mission import MissionRepository
from app.repositories.planning_run import PlanningRunRepository
from app.services.event_factory import EventFactory

logger = logging.getLogger(__name__)


class MissionPlanner:
    SYSTEM_PROMPT = (
        "You are Forge's bounded project planner. Create the smallest useful team and Task DAG "
        "for the user-supplied Mission. Respect the supplied capabilities and limits. Do not "
        "assume unavailable tools, do not claim work has happened, and provide measurable "
        "acceptance criteria. For user-facing frontend work, include measurable product criteria "
        "covering the 1440x900, 768x1024, and 390x844 viewport contract: no unintended "
        "horizontal overflow, readable wrapping, associated form labels, visible keyboard focus, "
        "and explicit loading/empty/error states where applicable. Do not claim screenshots or "
        "browser checks are available. You only propose structured data; you never mutate "
        "databases."
    )

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        provider: ModelProvider | None = None,
        registry: ModelRegistry | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider
        self.registry = registry or ModelRegistry.from_settings(self.settings)
        self.router = ModelRouter(self.registry.profiles)
        self.validator = validator or PlanValidator(self.settings, model_registry=self.registry)
        self.policy = ModelExecutionPolicy(self.settings)
        self.costs = CostEstimator(self.settings.model_pricing)
        self.missions = MissionRepository(session)
        self.runs = PlanningRunRepository(session)
        self.events = EventFactory(session)
        self.economics = ModelEconomicsService(session, self.settings)

    async def plan(self, mission_id: UUID) -> PlanningRun:
        resolved = self.registry.resolve("planner")
        mission = await self.missions.get(mission_id)
        if mission is None:
            raise EntityNotFoundError("Mission")
        selections, capabilities = await self._route_models(
            mission, resolved.alias, resolved.model_id
        )
        mission, run = await self._start(mission_id, selections[0], capabilities)
        started = perf_counter()
        request = self._request(mission, selections[0].profile.model_id)
        try:
            response = await self._generate_with_retry(
                run.id,
                request,
                mission,
                selections,
                capabilities,
            )
        except ProviderCallError as exc:
            failed = await self._fail(mission_id, run.id, exc)
            raise MissionPlanningError(
                exc.code,
                exc.message,
                self._status_code(exc),
                details=self._failure_details(failed, exc),
            ) from None

        try:
            proposal = (
                response.output
                if isinstance(response.output, PlanProposal)
                else PlanProposal.model_validate(response.output)
            )
        except ValidationError:
            validation = PlanValidationResult(
                valid=False,
                errors=[
                    PlanValidationError(
                        code="INVALID_PLAN",
                        message="Planner output did not match the PlanProposal contract.",
                    )
                ],
                agent_count=0,
                task_count=0,
                dependency_count=0,
            )
            completed = await self._complete_invalid(
                mission_id,
                run.id,
                None,
                validation,
                response,
                error_code="PLANNER_SCHEMA_VALIDATION_FAILED",
                error_message="Planner output did not match the required plan schema.",
            )
            self._log(completed, started, validation)
            return completed

        validation = self.validator.validate(proposal)
        if not validation.valid:
            completed = await self._complete_invalid(
                mission_id, run.id, proposal, validation, response
            )
            self._log(completed, started, validation)
            return completed
        completed = await self._complete_success(mission_id, run.id, proposal, validation, response)
        self._log(completed, started, validation)
        return completed

    async def _route_models(
        self,
        mission: Mission,
        requested_alias: str,
        model_id: str,
    ) -> tuple[list[ModelSelection], frozenset[ModelCapability]]:
        capabilities = required_capabilities(
            role="PLANNER", has_tools=False, structured_output=True
        )
        if self.provider is not None:
            self.policy.authorize(self.provider)
            profile = ModelProfile(
                alias=requested_alias,
                provider=self.provider.name,
                model_id=model_id,
                tier=EconomicTier.PREMIUM if self.provider.paid else EconomicTier.FREE,
                capabilities=frozenset(ModelCapability),
                paid=self.provider.paid,
                max_call_cost=0.05 if self.provider.paid else 0,
            )
            return [
                ModelSelection(profile, "Explicit test/development provider override selected.")
            ], capabilities
        snapshot = await self.economics.snapshot(mission=mission)
        health = await self.economics.provider_health(self.registry.profiles)
        selections = self.router.candidates(
            RoutingContext(
                required_capabilities=capabilities,
                paid_budget_remaining=float(snapshot.paid_budget_remaining),
                requested_alias=requested_alias,
                provider_health=health,
            )
        )
        return selections, capabilities

    async def _generate_with_retry(
        self,
        run_id: UUID,
        request: ModelRequest,
        mission: Mission,
        selections: list[ModelSelection],
        capabilities: frozenset[ModelCapability],
    ) -> ModelResponse:
        max_attempts = max(self.settings.planner_provider_max_attempts, 1)
        total_attempt = 0
        previous: ModelSelection | None = None
        final_error = ProviderCallError(
            "PLANNER_PROVIDER_ERROR", "No planning provider was available."
        )
        for selection_index, selection in enumerate(selections):
            provider = (
                self.provider
                if self.provider is not None
                else provider_for_profile(self.settings, selection.profile)
            )
            assert provider is not None
            try:
                self.policy.authorize(provider)
            except Exception as exc:
                final_error = ProviderCallError(
                    getattr(exc, "code", "MODEL_CONFIGURATION_ERROR"),
                    getattr(exc, "message", "The planning provider is unavailable."),
                    category="CONFIGURATION",
                )
                previous = selection
                continue
            if selection_index:
                await self._record_fallback(run_id, previous, selection, final_error)
            current_request = replace(request, model=selection.profile.model_id)
            for attempt in range(1, max_attempts + 1):
                total_attempt += 1
                call = await self.economics.begin_call(
                    selection,
                    capabilities=frozenset(item.value for item in capabilities),
                    agent_role="PLANNER",
                    mission=mission,
                    planning_run_id=run_id,
                    fallback_from=(
                        (previous.profile.provider, previous.profile.model_id) if previous else None
                    ),
                    fallback_reason=final_error.code if previous else None,
                )
                try:
                    async with asyncio.timeout(self.settings.planner_request_timeout_seconds):
                        response = await provider.generate(current_request)
                except TimeoutError:
                    raw_error = ProviderCallError(
                        "MODEL_TIMEOUT",
                        "The planning provider request timed out.",
                        retryable=True,
                        category="TIMEOUT",
                        exception_type="TimeoutError",
                    )
                except ProviderCallError as provider_error:
                    raw_error = provider_error
                except Exception as exc:
                    logger.error(
                        "Unexpected Mission planning provider error",
                        extra={
                            "event": "mission_planning_provider_error",
                            "planning_run_id": str(run_id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    raw_error = ProviderCallError(
                        "MODEL_PROVIDER_ERROR",
                        "The planning provider request failed unexpectedly.",
                        category="INTERNAL",
                        exception_type=type(exc).__name__,
                    )
                else:
                    await self.economics.complete_call(call.id, response=response)
                    await self._record_provider_attempt(
                        run_id, total_attempt, None, outcome="SUCCEEDED"
                    )
                    return response
                await self.economics.complete_call(call.id, error=raw_error)
                error = self._normalize_provider_error(raw_error)
                final_error = error
                should_retry = error.retryable and attempt < max_attempts
                await self._record_provider_attempt(
                    run_id,
                    total_attempt,
                    error,
                    outcome="RETRY_SCHEDULED" if should_retry else "FAILED",
                )
                if should_retry:
                    await asyncio.sleep(
                        self.settings.planner_retry_base_seconds * (2 ** max(attempt - 1, 0))
                    )
                    continue
                break
            previous = selection
        raise final_error

    async def _record_fallback(
        self,
        run_id: UUID,
        previous: ModelSelection | None,
        selected: ModelSelection,
        error: ProviderCallError,
    ) -> None:
        run = await self.runs.get(run_id)
        if run is None or run.status != PlanningRunStatus.RUNNING:
            raise MissionPlanningError("PLANNER_STATE_LOST", "Planning state was not found", 409)
        run.fallback_history = [
            *run.fallback_history,
            {
                "at": datetime.now(UTC).isoformat(),
                "from_provider": previous.profile.provider if previous else run.provider,
                "from_model": previous.profile.model_id if previous else run.resolved_model,
                "to_provider": selected.profile.provider,
                "to_model": selected.profile.model_id,
                "reason": error.code,
            },
        ][-10:]
        run.provider = selected.profile.provider
        run.model_alias = selected.profile.alias
        run.resolved_model = selected.profile.model_id
        run.economic_tier = selected.profile.tier.value
        run.selection_reason = selected.reason
        await self.session.commit()

    async def _record_provider_attempt(
        self,
        run_id: UUID,
        attempt: int,
        error: ProviderCallError | None,
        *,
        outcome: str,
    ) -> None:
        run = await self.runs.get(run_id)
        if run is None or run.status != PlanningRunStatus.RUNNING:
            raise MissionPlanningError("PLANNER_STATE_LOST", "Planning state was not found", 409)
        run.provider_attempts = attempt
        history = list(run.retry_history or [])
        entry: dict[str, object] = {
            "attempt": attempt,
            "at": datetime.now(UTC).isoformat(),
            "outcome": outcome,
        }
        if error is not None:
            entry.update(
                {
                    "code": error.code,
                    "message": error.message,
                    "category": error.category,
                    "retryable": error.retryable,
                }
            )
            run.internal_diagnostics = {
                "phase": "PROVIDER_REQUEST",
                "provider": run.provider,
                "model": run.resolved_model,
                "latest_error": error.diagnostics(),
            }
        run.retry_history = [*history, entry][-10:]
        if outcome == "RETRY_SCHEDULED":
            mission = await self.missions.get(run.mission_id)
            await self.events.create(
                event_type="PLANNING_PROVIDER_RETRY_SCHEDULED",
                message="Transient planning-provider retry scheduled.",
                payload={
                    "mission_id": str(run.mission_id),
                    "planning_run_id": str(run.id),
                    "code": error.code if error else None,
                    "attempt": attempt,
                    "max_attempts": max(self.settings.planner_provider_max_attempts, 1),
                },
                company_id=mission.company_id if mission else None,
                correlation_id=run.mission_id,
            )
        await self.session.commit()

    async def _start(
        self,
        mission_id: UUID,
        selection: ModelSelection,
        capabilities: frozenset[ModelCapability],
    ) -> tuple[Mission, PlanningRun]:
        try:
            mission = await self.missions.get_for_update(mission_id)
            if mission is None:
                raise EntityNotFoundError("Mission")
            # Routing performs a read before this lock. Refresh after acquiring it
            # so concurrent planners cannot act on an identity-map-cached DRAFT.
            await self.session.refresh(mission)
            if mission.archived_at is not None:
                raise MissionConflictError(
                    "MISSION_ARCHIVED", "Restore the Mission before planning it."
                )
            if mission.status != MissionStatus.DRAFT:
                raise MissionConflictError(
                    "MISSION_NOT_PLANNABLE",
                    f"Mission in {mission.status.value} cannot start planning.",
                )
            if mission.planning_attempts >= mission.max_planning_attempts:
                raise MissionConflictError(
                    "MISSION_PLANNING_ATTEMPTS_EXHAUSTED",
                    "Mission has reached its maximum planning attempts.",
                )
            now = datetime.now(UTC)
            mission.status = MissionStatus.PLANNING
            mission.planning_attempts += 1
            mission.planning_started_at = now
            mission.planning_completed_at = None
            mission.failure_reason = None
            mission.failed_at = None
            run = await self.runs.add(
                PlanningRun(
                    mission_id=mission.id,
                    status=PlanningRunStatus.RUNNING,
                    provider=selection.profile.provider,
                    model_alias=selection.profile.alias,
                    resolved_model=selection.profile.model_id,
                    economic_tier=selection.profile.tier.value,
                    selection_reason=selection.reason,
                    required_capabilities=sorted(item.value for item in capabilities),
                    started_at=now,
                )
            )
            await self.events.create(
                event_type="MISSION_PLANNING_STARTED",
                message="Mission planning started.",
                payload={"mission_id": str(mission.id), "planning_run_id": str(run.id)},
                company_id=mission.company_id,
                correlation_id=mission.id,
            )
            await self.events.create(
                event_type="PLANNING_RUN_STARTED",
                message="Planning run started.",
                payload={
                    "mission_id": str(mission.id),
                    "planning_run_id": str(run.id),
                    "provider": selection.profile.provider,
                    "model_alias": selection.profile.alias,
                    "selection_reason": selection.reason,
                },
                company_id=mission.company_id,
                correlation_id=mission.id,
            )
            await self.session.commit()
            return mission, run
        except Exception:
            await self.session.rollback()
            raise

    def _request(self, mission: Mission, model_id: str) -> ModelRequest:
        planner_input = {
            "mission": {
                "title": mission.title,
                "goal": mission.goal,
                "constraints": mission.constraints,
                "context": mission.context,
            },
            "capabilities": self.validator.capability_manifest,
            "limits": {
                "max_agents": self.settings.mission_max_agents,
                "max_tasks": self.settings.mission_max_tasks,
                "max_dependencies": self.settings.mission_max_dependencies,
            },
            "runtime_boundaries": {
                "internet": False,
                "browser": False,
                "shell": False,
                "email": False,
                "deployment": False,
            },
        }
        return ModelRequest(
            model=model_id,
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt="Create a validated project proposal:\n"
            + json.dumps(planner_input, sort_keys=True, separators=(",", ":")),
            metadata={
                "mission_id": str(mission.id),
                "mission_title": mission.title,
                "planning_attempt": str(mission.planning_attempts),
            },
            response_model=PlanProposal,
        )

    async def _complete_success(
        self,
        mission_id: UUID,
        run_id: UUID,
        proposal: PlanProposal,
        validation: PlanValidationResult,
        response: ModelResponse,
    ) -> PlanningRun:
        mission, run, now = await self._locked_completion(mission_id, run_id)
        run.status = PlanningRunStatus.SUCCEEDED
        run.proposal = proposal.model_dump(mode="json")
        run.validation_result = validation.model_dump(mode="json")
        self._usage(run, response)
        run.completed_at = now
        mission.status = MissionStatus.PLAN_READY
        mission.planning_completed_at = now
        await self.events.create(
            event_type="PLANNING_RUN_SUCCEEDED",
            message="Planning run produced a valid proposal.",
            payload={"mission_id": str(mission.id), "planning_run_id": str(run.id)},
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.events.create(
            event_type="MISSION_PLAN_READY",
            message="Mission plan is ready for human activation.",
            payload={"mission_id": str(mission.id), "planning_run_id": str(run.id)},
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.session.commit()
        return run

    async def _complete_invalid(
        self,
        mission_id: UUID,
        run_id: UUID,
        proposal: PlanProposal | None,
        validation: PlanValidationResult,
        response: ModelResponse,
        *,
        error_code: str = "INVALID_PLAN",
        error_message: str = "Plan validation failed.",
    ) -> PlanningRun:
        mission, run, now = await self._locked_completion(mission_id, run_id)
        run.status = PlanningRunStatus.INVALID
        run.proposal = proposal.model_dump(mode="json") if proposal else None
        run.validation_result = validation.model_dump(mode="json")
        run.error = {
            "code": error_code,
            "message": error_message,
            "category": "SCHEMA_VALIDATION"
            if error_code == "PLANNER_SCHEMA_VALIDATION_FAILED"
            else "PLAN_VALIDATION",
            "phase": "RESPONSE_VALIDATION",
            "retryable": False,
        }
        self._usage(run, response)
        run.completed_at = now
        self._replannable_or_failed(mission, error_code, now)
        await self.events.create(
            event_type="PLANNING_RUN_INVALID",
            message="Planning run produced an invalid proposal.",
            payload={
                "mission_id": str(mission.id),
                "planning_run_id": str(run.id),
                "validation_errors": [error.model_dump() for error in validation.errors],
            },
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.events.create(
            event_type="MISSION_PLANNING_FAILED",
            message="Mission plan validation failed; no topology was created.",
            payload={"mission_id": str(mission.id), "code": error_code},
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.session.commit()
        return run

    async def _fail(self, mission_id: UUID, run_id: UUID, error: ProviderCallError) -> PlanningRun:
        mission, run, now = await self._locked_completion(mission_id, run_id)
        run.status = PlanningRunStatus.FAILED
        run.error = self._error_payload(run, error)
        run.completed_at = now
        self._provider_failure_replannable(mission, error.code, now)
        await self.events.create(
            event_type="PLANNING_RUN_FAILED",
            message="Planning run failed.",
            payload={
                "mission_id": str(mission.id),
                "planning_run_id": str(run.id),
                "code": error.code,
            },
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.events.create(
            event_type="MISSION_PLANNING_FAILED",
            message="Mission planning failed without materializing a plan.",
            payload={"mission_id": str(mission.id), "code": error.code},
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.session.commit()
        logger.warning(
            "Mission planning failed",
            extra={
                "event": "mission_planning_failed",
                "mission_id": str(mission.id),
                "planning_run_id": str(run.id),
                "provider": run.provider,
                "model_id": run.resolved_model,
                "error_code": error.code,
                "error_category": error.category,
                "provider_status": error.http_status,
                "provider_error_code": error.provider_error_code,
                "provider_attempts": run.provider_attempts,
                "retryable": error.retryable,
            },
        )
        return run

    async def _locked_completion(
        self, mission_id: UUID, run_id: UUID
    ) -> tuple[Mission, PlanningRun, datetime]:
        mission = await self.missions.get_for_update(mission_id)
        run = await self.runs.get(run_id)
        if mission is None or run is None:
            await self.session.rollback()
            raise MissionPlanningError("PLANNER_STATE_LOST", "Planning state was not found")
        if mission.status != MissionStatus.PLANNING or run.status != PlanningRunStatus.RUNNING:
            await self.session.rollback()
            raise MissionConflictError(
                "MISSION_PLANNING_CONFLICT", "Mission planning state changed concurrently."
            )
        return mission, run, datetime.now(UTC)

    def _usage(self, run: PlanningRun, response: ModelResponse) -> None:
        run.input_tokens = response.usage.input_tokens
        run.output_tokens = response.usage.output_tokens
        run.cached_tokens = response.usage.cached_input_tokens
        run.estimated_cost = self.costs.estimate(
            response.provider or run.provider,
            response.model or run.resolved_model,
            response.usage,
        )

    @staticmethod
    def _replannable_or_failed(mission: Mission, reason: str, now: datetime) -> None:
        mission.planning_completed_at = now
        mission.failure_reason = reason
        if mission.planning_attempts >= mission.max_planning_attempts:
            mission.status = MissionStatus.FAILED
            mission.failed_at = now
        else:
            mission.status = MissionStatus.DRAFT

    @staticmethod
    def _provider_failure_replannable(mission: Mission, reason: str, now: datetime) -> None:
        """Release the business planning attempt consumed before provider I/O.

        A provider/runtime failure did not produce a candidate plan for review, so it
        must not exhaust the mission's semantic planning budget. Automatic provider
        retries remain bounded independently on the PlanningRun.
        """
        mission.planning_attempts = max(mission.planning_attempts - 1, 0)
        mission.planning_completed_at = now
        mission.failure_reason = reason
        mission.failed_at = None
        mission.status = MissionStatus.DRAFT

    @staticmethod
    def _normalize_provider_error(error: ProviderCallError) -> ProviderCallError:
        mapping = {
            "MODEL_TIMEOUT": (
                "PLANNER_TIMEOUT",
                "The planning provider request timed out.",
                True,
            ),
            "MODEL_RATE_LIMIT": (
                "PLANNER_RATE_LIMITED",
                "The planning provider rate limit was reached.",
                True,
            ),
            "MODEL_PROVIDER_TEMPORARY": (
                "PLANNER_PROVIDER_TEMPORARY",
                "The planning provider is temporarily unavailable.",
                True,
            ),
            "MODEL_QUOTA_EXHAUSTED": (
                "PLANNER_QUOTA_EXHAUSTED",
                "The planning provider API credit balance is exhausted.",
                False,
            ),
            "MODEL_AUTH_ERROR": (
                "PLANNER_AUTHENTICATION_FAILED",
                "The planning provider rejected authentication.",
                False,
            ),
            "MODEL_CONFIGURATION_ERROR": (
                "PLANNER_CONFIGURATION_ERROR",
                "The planning provider rejected the configured request.",
                False,
            ),
            "INVALID_MODEL_OUTPUT": (
                "PLANNER_MALFORMED_RESPONSE",
                "The planning provider returned a malformed structured response.",
                False,
            ),
            "MODEL_PROVIDER_ERROR": (
                "PLANNER_PROVIDER_ERROR",
                "The planning provider request failed.",
                error.retryable,
            ),
        }
        code, message, retryable = mapping.get(
            error.code,
            (
                "PLANNER_PROVIDER_ERROR",
                "The planning provider request failed.",
                False,
            ),
        )
        return ProviderCallError(
            code,
            message,
            retryable=retryable,
            category=error.category,
            http_status=error.http_status,
            provider_error_code=error.provider_error_code,
            provider_error_type=error.provider_error_type,
            request_id=error.request_id,
            exception_type=error.exception_type,
            details=error.details,
        )

    def _error_payload(self, run: PlanningRun, error: ProviderCallError) -> dict[str, object]:
        attempts = run.provider_attempts
        maximum = max(self.settings.planner_provider_max_attempts, 1)
        payload: dict[str, object] = {
            "code": error.code,
            "message": error.message,
            "category": error.category,
            "phase": "PROVIDER_REQUEST",
            "retryable": error.retryable,
            "provider_attempts": attempts,
            "max_provider_attempts": maximum,
            "retry_exhausted": error.retryable and attempts >= maximum,
            "mission_attempt_consumed": False,
        }
        if error.http_status is not None:
            payload["provider_status"] = error.http_status
        if error.provider_error_code is not None:
            payload["provider_error_code"] = error.provider_error_code
        payload.update(error.safe_details())
        return payload

    def _failure_details(self, run: PlanningRun, error: ProviderCallError) -> dict[str, object]:
        return {
            **self._error_payload(run, error),
            "planning_run_id": str(run.id),
            "mission_id": str(run.mission_id),
        }

    @staticmethod
    def _status_code(error: ProviderCallError) -> int:
        return {
            "PLANNER_TIMEOUT": 504,
            "PLANNER_RATE_LIMITED": 429,
            "PLANNER_PROVIDER_TEMPORARY": 503,
            "PLANNER_QUOTA_EXHAUSTED": 402,
            "PLANNER_AUTHENTICATION_FAILED": 502,
            "PLANNER_CONFIGURATION_ERROR": 502,
            "PLANNER_MALFORMED_RESPONSE": 502,
        }.get(error.code, 502)

    def _log(self, run: PlanningRun, started: float, validation: PlanValidationResult) -> None:
        logger.info(
            "Mission planning completed",
            extra={
                "event": "mission_planning_completed",
                "mission_id": str(run.mission_id),
                "planning_run_id": str(run.id),
                "provider": run.provider,
                "model_alias": run.model_alias,
                "status": run.status.value,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "agent_count": validation.agent_count,
                "task_count": validation.task_count,
                "dependency_count": validation.dependency_count,
            },
        )

    async def recover_stale(self, limit: int = 100) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.planning_run_stale_seconds)
        runs = await self.runs.stale_running(cutoff, limit)
        now = datetime.now(UTC)
        recovered = 0
        for run in runs:
            mission = await self.missions.get_for_update(run.mission_id)
            run.status = PlanningRunStatus.FAILED
            run.completed_at = now
            run.error = {
                "code": "PLANNER_TIMEOUT",
                "message": "Stale planning run was recovered after process interruption.",
                "category": "RECOVERY",
                "phase": "PROVIDER_REQUEST",
                "retryable": True,
                "provider_attempts": run.provider_attempts,
                "max_provider_attempts": max(self.settings.planner_provider_max_attempts, 1),
                "retry_exhausted": False,
            }
            run.internal_diagnostics = {
                "phase": "PROVIDER_REQUEST",
                "recovered": True,
                "reason": "STALE_PLANNING_RUN",
            }
            if mission is not None and mission.status == MissionStatus.PLANNING:
                self._provider_failure_replannable(mission, "PLANNER_TIMEOUT", now)
            await self.events.create(
                event_type="PLANNING_RUN_FAILED",
                message="Stale planning run was recovered as failed.",
                payload={
                    "mission_id": str(run.mission_id),
                    "planning_run_id": str(run.id),
                    "code": "PLANNER_TIMEOUT",
                    "recovered": True,
                },
                company_id=mission.company_id if mission else None,
                correlation_id=run.mission_id,
            )
            recovered += 1
        await self.session.commit()
        return recovered
