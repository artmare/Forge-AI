import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.builders import ContextBuilder, ContextRecord, InstructionBuilder
from app.agent_runtime.context_rollover import ContextRollover, mutation_key
from app.agent_runtime.contracts import (
    BaseAgentResult,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelTool,
    ModelToolCall,
    ModelToolExchange,
    ProviderCallError,
)
from app.agent_runtime.cost import CostEstimator
from app.agent_runtime.dev_models import (
    AccountedProbeProvider,
    catalog_for,
    prober_for,
    refresh_profiles,
)
from app.agent_runtime.economics import ModelEconomicsService
from app.agent_runtime.efficiency import EfficientRuntimeService, ModelBudgetExceeded
from app.agent_runtime.execution_truth import ExecutionTruthValidator
from app.agent_runtime.policy import ModelExecutionPolicy
from app.agent_runtime.probes import ProbeCapability
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
from app.domain.enums import (
    AgentRunStatus,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.exceptions import AgentRuntimeDomainError, TaskRunStateConflictError
from app.domain.models import AgentRun
from app.repositories.agent import AgentRepository
from app.repositories.agent_run import AgentRunRepository
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.services.event_factory import EventFactory
from app.services.project_knowledge import ProjectKnowledgeService
from app.services.task_state_machine import TaskStateMachine
from app.tool_system.contracts import (
    AgentTurnResponse,
    FinalTurn,
    ToolExecutionContext,
    ToolObservation,
    ToolRequestTurn,
)
from app.tool_system.permissions import PermissionEngine
from app.tool_system.registry import ToolRegistry
from app.tool_system.service import ToolExecutionService

logger = logging.getLogger(__name__)

NORMALIZED_PROVIDER_CODES = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_AUTH_ERROR",
        "MODEL_RATE_LIMIT",
        "MODEL_QUOTA_EXHAUSTED",
        "MODEL_CONFIGURATION_ERROR",
        "MODEL_PROVIDER_TEMPORARY",
        "MODEL_PROVIDER_ERROR",
        "MODEL_PROVIDER_DISABLED",
        "MODEL_PROVIDER_UNAVAILABLE",
        "MODEL_UNAVAILABLE",
        "MODEL_CAPABILITY_UNAVAILABLE",
        "MODEL_ESCALATION_REQUIRED",
        "MODEL_BUDGET_UNAVAILABLE",
        "MODEL_CALL_BUDGET_EXHAUSTED",
        "MISSION_MODEL_CALL_LIMIT_EXHAUSTED",
        "FREE_MODEL_CALL_LIMIT_EXHAUSTED",
        "PAID_MODEL_CALL_LIMIT_EXHAUSTED",
        "MODEL_ACCOUNTING_STATE_LOST",
        "INVALID_MODEL_OUTPUT",
        "MAX_TOOL_STEPS_EXCEEDED",
        "AGENT_EXECUTION_CANCELLED",
        "AGENT_RUNTIME_ERROR",
        "MODEL_BUDGET_EXHAUSTED",
        "DUPLICATE_TOOL_LOOP",
        "PROVIDER_REQUEST_INVALID",
        "PROVIDER_RESPONSE_INVALID",
        "PROVIDER_TOOL_SCHEMA_INVALID",
        "PROVIDER_TOOL_PROTOCOL_ERROR",
        "UNVERIFIED_EXECUTION_CLAIM",
    }
)


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        provider: ModelProvider | None = None,
        registry: ModelRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        execution_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider
        self.registry = registry or ModelRegistry.from_settings(self.settings)
        self.router = ModelRouter(self.registry.profiles)
        self.tool_registry = tool_registry or ToolRegistry.from_settings(self.settings, session)
        self.execution_guard = execution_guard
        self.policy = ModelExecutionPolicy(self.settings)
        self.costs = CostEstimator(self.settings.model_pricing)
        self.agent_runs = AgentRunRepository(session)
        self.agents = AgentRepository(session)
        self.tasks = TaskRepository(session)
        self.task_runs = TaskRunRepository(session)
        self.events = EventFactory(session)
        self.permission_engine = PermissionEngine(session)
        self.tool_execution = ToolExecutionService(
            session, settings=self.settings, registry=self.tool_registry
        )
        self.efficiency = EfficientRuntimeService(session, self.settings)
        self.economics = ModelEconomicsService(session, self.settings)

    async def execute(
        self,
        task_id: UUID,
        model_alias: str = "default",
        *,
        defer_review: bool = False,
    ) -> AgentRun:
        execution_started = perf_counter()
        if self.provider is not None:
            self.policy.authorize(self.provider)
        from app.development.self_workflow import SelfDevelopmentWorkflow

        initial_task = await self.tasks.get_current(task_id)
        if initial_task is not None and SelfDevelopmentWorkflow.requested(initial_task):
            await SelfDevelopmentWorkflow(self.session, self.settings).prepare(initial_task)
        context = await ContextBuilder(self.session).build(task_id)
        observations: list[ToolObservation] = []
        tool_exchanges: list[ModelToolExchange] = []
        conversation_start_prompt: str | None = None
        tool_steps = 0
        first_turn = True
        duplicate_signals = 0
        rollover = ContextRollover(
            self.settings.forge_dev_context_checkpoint_ratio, self.settings.forge_dev_max_rollovers
        )

        while True:
            await self._assert_execution_allowed()
            current_task = await self.tasks.get_current(task_id)
            if current_task is None:
                raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task was not found")
            try:
                budget = await self.efficiency.enforce_budget(current_task)
            except ModelBudgetExceeded as exc:
                await self.session.commit()
                raise AgentRuntimeDomainError(
                    "MODEL_BUDGET_EXHAUSTED",
                    f"Task stopped for human intervention: {exc.reason}",
                    409,
                ) from None
            routed_alias = await self.efficiency.route(
                current_task,
                str(context.agent.get("role", "GENERAL")),
                model_alias,
                duplicate_signals,
            )
            resolved = self.registry.resolve(routed_alias)
            request = await self._build_request(
                context,
                resolved.alias,
                resolved.model_id,
                observations,
                turn_number=tool_steps + 1,
                budget_warning=budget.warning_active,
                duplicate_warning=duplicate_signals > 0,
                force_final=self._developer_ready_for_final(context, observations),
            )
            if rollover.handoff:
                request = replace(
                    request,
                    user_prompt=request.user_prompt
                    + "\nForge context handoff:\n"
                    + json.dumps(rollover.handoff),
                )
            if conversation_start_prompt is None:
                conversation_start_prompt = request.user_prompt
            request = replace(
                request,
                conversation_start_prompt=conversation_start_prompt,
                tool_exchanges=tuple(tool_exchanges),
            )
            try:
                selections, capabilities = await self._route_models(
                    current_task,
                    str(context.agent.get("role", "GENERAL")),
                    routed_alias,
                    request,
                    duplicate_signals,
                )
            except ProviderCallError as error:
                raise self._public_error(self._normalize_error(error)) from None
            selected = selections[0]
            request = replace(request, model=selected.profile.model_id)
            if (
                self.settings.forge_dev_mode_enabled
                and tool_exchanges
                and rollover.needed(
                    request,
                    selected.profile.context_length or self.settings.forge_dev_context_limit,
                )
            ):
                try:
                    handoff = rollover.checkpoint(
                        task_id=str(task_id),
                        goal=str(context.task.get("title", "")),
                        observations=observations,
                        tool_steps=tool_steps,
                        duplicate_signals=duplicate_signals,
                    )
                except ValueError:
                    raise AgentRuntimeDomainError(
                        "MODEL_BUDGET_EXHAUSTED", "Context rollover bound reached", 409
                    ) from None
                if current_task.project_id:
                    await ProjectKnowledgeService(self.session).checkpoint(
                        current_task.project_id,
                        task_id=task_id,
                        goal=handoff["goal"],
                        completed=[json.dumps(x) for x in handoff["completed"]],
                        current_diff=handoff["current_diff"],
                        decisions=[],
                        test_status=[json.dumps(x) for x in handoff["test_status"]],
                        failures=[json.dumps(x) for x in handoff["failures"]],
                        open_questions=[],
                        next_action=handoff["next_action"],
                    )
                await self.events.create(
                    company_id=current_task.company_id,
                    project_id=current_task.project_id,
                    task_id=task_id,
                    correlation_id=task_id,
                    event_type="DEV_CONTEXT_ROLLOVER",
                    message="Fresh model context prepared after durable tool observation.",
                    payload=handoff,
                )
                await self.session.commit()
                # Only transport history is dropped. Evidence, counters, budgets and task remain.
                tool_exchanges.clear()
                context = await ContextBuilder(self.session).build(task_id)
                fresh = await self._build_request(
                    context,
                    routed_alias,
                    selected.profile.model_id,
                    observations,
                    turn_number=tool_steps + 1,
                    budget_warning=budget.warning_active,
                    duplicate_warning=duplicate_signals > 0,
                    force_final=self._developer_ready_for_final(context, observations),
                )
                conversation_start_prompt = (
                    fresh.user_prompt + "\nForge context handoff:\n" + json.dumps(handoff)
                )
                request = replace(
                    fresh,
                    user_prompt=conversation_start_prompt,
                    conversation_start_prompt=conversation_start_prompt,
                )

            run = await self._start_turn(
                task_id,
                selected,
                capabilities,
                request,
                first_turn=first_turn,
            )
            first_turn = False
            provider_response = await self._generate(
                run,
                request,
                selections,
                capabilities,
                current_task,
                str(context.agent.get("role", "GENERAL")),
                validate_response=partial(
                    self._validate_execution_truth_response,
                    context=context,
                    observations=observations,
                ),
            )
            if not await self._execution_allowed():
                error = ProviderCallError("AGENT_RUNTIME_ERROR", "Durable execution lease was lost")
                await self._fail(run.id, error)
                raise self._public_error(error)
            try:
                turn = self._parse_turn(provider_response.output)
            except ValidationError:
                error = ProviderCallError(
                    "INVALID_MODEL_OUTPUT", "The model returned an invalid structured turn"
                )
                await self._fail(run.id, error)
                raise self._public_error(error) from None

            task = await self.tasks.get_current(task_id)
            if task is None:
                error = ProviderCallError("AGENT_RUNTIME_ERROR", "Task was not found")
                await self._fail(run.id, error)
                raise self._public_error(error)
            if task.status == TaskStatus.CANCELLED:
                error = ProviderCallError(
                    "AGENT_EXECUTION_CANCELLED", "Task was cancelled during agent execution"
                )
                await self._cancel_run(run.id, error)
                raise self._public_error(error)
            if task.status != TaskStatus.IN_PROGRESS:
                error = ProviderCallError("AGENT_RUNTIME_ERROR", "Task execution state is invalid")
                await self._fail(run.id, error)
                raise self._public_error(error)

            if isinstance(turn, FinalTurn):
                truth = ExecutionTruthValidator.validate(context, turn.result, observations)
                if not truth.accepted:
                    error = ProviderCallError(
                        truth.code,
                        truth.message,
                        category="TOOL_PROTOCOL",
                    )
                    await self._fail(run.id, error)
                    raise self._public_error(error)
                completed = await self._complete_turn(
                    run.id,
                    turn.result.model_dump(mode="json"),
                    provider_response,
                    final_result=turn.result,
                    transition_to_review=not defer_review,
                )
                self._log_succeeded(completed, execution_started, "final")
                return completed

            key = mutation_key(turn.tool_name, dict(turn.arguments))
            if rollover.count and key and key in rollover.executed_mutations:
                error = ProviderCallError("DUPLICATE_TOOL_LOOP", "Rollover mutation replay blocked")
                await self._fail(run.id, error)
                raise self._public_error(error)

            max_tool_steps = (
                self.settings.developer_max_steps
                if str(context.agent.get("role", "")).upper()
                in {"DEVELOPER", "LEAD_ENGINEER", "LEAD ENGINEER"}
                else self.settings.agent_max_tool_steps
            )
            if tool_steps >= max_tool_steps:
                error = ProviderCallError(
                    "MAX_TOOL_STEPS_EXCEEDED",
                    f"Agent exceeded the maximum of {max_tool_steps} tool steps",
                )
                await self._fail(run.id, error)
                raise self._public_error(error)

            completed = await self._complete_turn(
                run.id,
                turn.model_dump(mode="json"),
                provider_response,
                final_result=None,
                transition_to_review=True,
            )
            self._log_succeeded(completed, execution_started, "tool_call")
            duplicate_signals = await self.efficiency.record_tool_request(task.id, turn)
            await self.session.commit()
            if duplicate_signals >= self.settings.duplicate_tool_turn_threshold:
                error = ProviderCallError(
                    "DUPLICATE_TOOL_LOOP",
                    "Agent repeated the same unchanged tool action; execution stopped for review",
                )
                await self._fail_task(
                    completed,
                    error,
                    details={
                        "phase": "EXECUTING",
                        "tool_name": turn.tool_name,
                        "safe_arguments": self._safe_tool_arguments(turn),
                        "consecutive_duplicates": duplicate_signals,
                    },
                )
                raise self._public_error(error)
            execution_context = ToolExecutionContext(
                company_id=task.company_id,
                project_id=task.project_id,
                task_id=task.id,
                task_run_id=completed.task_run_id,
                agent_id=completed.agent_id,
                agent_run_id=completed.id,
            )
            if not await self._execution_allowed():
                error = ProviderCallError("AGENT_RUNTIME_ERROR", "Durable execution lease was lost")
                await self._fail_task(completed, error)
                raise self._public_error(error)
            try:
                _, observation = await self.tool_execution.request_and_execute(
                    turn, execution_context
                )
            except Exception as exc:
                logger.error(
                    "Unexpected tool orchestration error",
                    extra={
                        "event": "tool_orchestration_error",
                        "agent_run_id": str(completed.id),
                        "task_id": str(task.id),
                        "error_type": type(exc).__name__,
                    },
                )
                error = ProviderCallError("AGENT_RUNTIME_ERROR", "Tool orchestration failed")
                await self._fail_task(completed, error)
                raise self._public_error(error) from None
            if key and observation.status == "success":
                rollover.executed_mutations.add(key)
            observations.append(observation)
            tool_call = provider_response.tool_call or ModelToolCall(
                name=turn.tool_name,
                arguments=dict(turn.arguments),
            )
            tool_exchanges.append(
                ModelToolExchange(
                    call=tool_call,
                    response=observation.model_dump(mode="json"),
                )
            )
            await self.efficiency.cache_observation(task, turn, observation)
            await self.session.commit()
            tool_steps += 1

    async def _execution_allowed(self) -> bool:
        return self.execution_guard is None or await self.execution_guard()

    async def _assert_execution_allowed(self) -> None:
        if not await self._execution_allowed():
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Durable execution lease was lost")

    async def _build_request(
        self,
        context: ContextRecord,
        model_alias: str,
        model_id: str,
        observations: list[ToolObservation],
        *,
        turn_number: int,
        budget_warning: bool,
        duplicate_warning: bool,
        force_final: bool = False,
    ) -> ModelRequest:
        agent_id = UUID(str(context.agent["id"]))
        agent = await self.agents.get_current(agent_id)
        if agent is None:
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Agent was not found")
        allowed = await self.permission_engine.list_allowed(
            agent=agent,
            definitions=self.tool_registry.list(enabled_only=True),
            has_project_workspace=context.project is not None,
        )
        if force_final:
            allowed = []
        instructions = InstructionBuilder().build(
            context,
            tools=[definition.public() for definition in allowed],
            observations=observations,
            turn_number=turn_number,
            recent_observation_limit=self.settings.context_recent_observations,
            budget_warning=budget_warning,
            duplicate_warning=duplicate_warning,
            completion_required=force_final,
        )
        task_input = context.task.get("input", {})
        mock_scenario = task_input.get("mock_scenario") if isinstance(task_input, dict) else None
        mock_path = task_input.get("path") if isinstance(task_input, dict) else None
        mock_content = task_input.get("content") if isinstance(task_input, dict) else None
        request = ModelRequest(
            model=model_id,
            system_prompt=instructions.system_prompt,
            user_prompt=instructions.user_prompt,
            metadata={
                "task_id": str(context.task["id"]),
                "task_title": str(context.task["title"]),
                "runtime_role": instructions.runtime_role,
                "model_alias": model_alias,
                "turn_number": str(turn_number),
                "tool_observation_count": str(len(observations)),
                "mock_scenario": str(mock_scenario or ""),
                "mock_path": str(mock_path or ""),
                "mock_content": str(mock_content or ""),
                "is_revision": str(
                    context.review is not None or context.qa_feedback is not None
                ).lower(),
                "review_id": str(context.review.get("review_id", "")) if context.review else "",
                "review_iteration": str(context.review.get("review_iteration", ""))
                if context.review
                else "",
                "qa_result_id": str(context.qa_feedback.get("qa_result_id", ""))
                if context.qa_feedback
                else "",
                "qa_iteration": str(context.qa_feedback.get("qa_iteration", ""))
                if context.qa_feedback
                else "",
            },
            response_model=AgentTurnResponse,
            tools=tuple(
                ModelTool(
                    name=definition.name,
                    description=definition.description,
                    input_model=definition.input_model,
                )
                for definition in allowed
            ),
        )
        sent_bytes = len(request.user_prompt.encode("utf-8"))
        baseline_bytes = sent_bytes
        if turn_number > 1:
            baseline = InstructionBuilder().build(
                context,
                tools=[definition.public() for definition in allowed],
                observations=observations,
                turn_number=1,
                recent_observation_limit=max(len(observations), 1),
            )
            baseline_bytes = len(baseline.user_prompt.encode("utf-8"))
        await self.efficiency.record_context(
            UUID(str(context.task["id"])),
            sent_bytes,
            baseline_bytes - sent_bytes,
        )
        return request

    @staticmethod
    def _developer_ready_for_final(
        context: ContextRecord, observations: list[ToolObservation]
    ) -> bool:
        """Require a final-only turn after this Developer run has enough durable evidence."""
        if str(context.agent.get("role", "")).upper() not in {
            "DEVELOPER",
            "LEAD_ENGINEER",
            "LEAD ENGINEER",
        }:
            return False
        successful = [
            item
            for item in observations
            if item.status == "success"
            and (
                item.tool
                not in {
                    "development.execute",
                    "development.install_dependencies",
                    "git.init",
                    "git.status",
                    "git.diff",
                    "git.log",
                    "git.commit",
                }
                or str((item.result or {}).get("status", "")).upper() == "SUCCEEDED"
            )
        ]
        tools = {item.tool for item in successful}
        if not {"git.status", "git.diff"}.issubset(tools):
            return False

        profile = context.development_profile
        available_actions: set[str] = (
            {str(action) for action in profile.get("available_actions", [])}
            if isinstance(profile, dict)
            else set()
        )
        executed_actions: set[str] = set()
        for observation in successful:
            result = observation.result or {}
            profile = result.get("profile_refresh")
            if isinstance(profile, dict):
                available_actions.update(
                    str(action) for action in profile.get("available_actions", [])
                )
            if observation.tool == "development.execute" and result.get("action"):
                executed_actions.add(str(result["action"]))
        required_actions = available_actions.intersection(
            {
                "NODE_TEST",
                "NODE_BUILD",
                "NODE_LINT",
                "NODE_TYPECHECK",
                "PYTHON_TEST",
                "PYTHON_LINT",
            }
        )
        return bool(required_actions) and required_actions.issubset(executed_actions)

    async def _route_models(
        self,
        task: Any,
        agent_role: str,
        requested_alias: str,
        request: ModelRequest,
        duplicate_signals: int,
    ) -> tuple[list[ModelSelection], frozenset[ModelCapability]]:
        capabilities = required_capabilities(
            role=agent_role,
            has_tools=bool(request.tools),
            structured_output=True,
        )
        if self.provider is not None:
            profile = ModelProfile(
                alias=requested_alias,
                provider=self.provider.name,
                model_id=request.model,
                tier=EconomicTier.PREMIUM if self.provider.paid else EconomicTier.FREE,
                capabilities=frozenset(ModelCapability),
                paid=self.provider.paid,
                max_call_cost=0.05 if self.provider.paid else 0,
            )
            return [
                ModelSelection(profile, "Explicit test/development provider override selected.")
            ], capabilities

        profiles = self.registry.profiles
        if (
            self.settings.forge_dev_mode_enabled
            and self.settings.openrouter_enabled
            and self.settings.openrouter_discovery_enabled
        ):
            profiles = await refresh_profiles(
                profiles,
                base_url=self.settings.openrouter_base_url,
                ttl=self.settings.openrouter_catalog_ttl_seconds,
                free_only=self.settings.forge_dev_free_only or self.settings.openrouter_free_only,
            )
        if self.settings.forge_dev_mode_enabled and self.settings.forge_dev_free_only:
            profiles = tuple(p for p in profiles if not p.paid and p.tier == EconomicTier.FREE)
        snapshot = await self.economics.snapshot(task=task)
        health = await self.economics.provider_health(self.registry.profiles)
        escalation_reason = None
        if duplicate_signals > 0:
            escalation_reason = "REPEATED_DETERMINISTIC_FAILURE"
        elif task.iteration > 1:
            escalation_reason = "BOUNDED_IMPLEMENTATION_FAILURE"
        selections = ModelRouter(profiles).candidates(
            RoutingContext(
                required_capabilities=capabilities,
                paid_budget_remaining=float(snapshot.paid_budget_remaining),
                requested_alias=requested_alias,
                escalation_reason=escalation_reason,
                provider_health=health,
            )
        )
        bounded = selections[: max(self.settings.model_fallback_max_candidates, 1)]
        if self.settings.forge_dev_mode_enabled:
            accepted = []
            rejected = []
            probe_results = []
            bounded_keys = {
                (selection.profile.provider, selection.profile.model_id)
                for selection in bounded
            }
            health_states = health
            for profile in profiles:
                reason = None
                if not profile.enabled:
                    reason = "DISABLED_OR_PRICE_UNVERIFIED"
                elif not profile.supports(capabilities):
                    reason = "DECLARED_CAPABILITY_MISSING"
                elif (
                    health_states.get((profile.provider, profile.model_id), "HEALTHY") != "HEALTHY"
                ):
                    reason = "HEALTH_COOLDOWN"
                elif (profile.provider, profile.model_id) not in bounded_keys:
                    reason = "FALLBACK_BOUND"
                if reason:
                    rejected.append(
                        {"provider": profile.provider, "model": profile.model_id, "reason": reason}
                    )
            prober = prober_for(
                self.settings.openrouter_base_url,
                self.settings.openrouter_probe_ttl_seconds,
                self.settings.model_provider_health_cooldown_seconds,
                self.settings.openrouter_probe_timeout_seconds,
            )
            for selection in bounded:
                if selection.profile.provider != "openrouter":
                    accepted.append(selection)
                    continue
                provider = provider_for_profile(self.settings, selection.profile)
                self.policy.authorize(provider)
                required = [ProbeCapability.STRUCTURED_OUTPUT]
                if request.tools:
                    required += [ProbeCapability.TOOL_CALLING, ProbeCapability.TOOL_CONTINUATION]
                valid = True
                for capability in required:
                    result = await prober.probe(
                        AccountedProbeProvider(provider, self.economics, selection, task),
                        selection.profile.model_id,
                        capability,
                        metadata={
                            "provider_supported_parameters": ",".join(
                                sorted(selection.profile.supported_parameters or ())
                            )
                        },
                    )
                    probe_results.append(
                        {
                            "provider": result.provider,
                            "model": result.model,
                            "capability": result.capability.value,
                            "status": result.status,
                            "timestamp": result.timestamp.isoformat(),
                            "retry_at": result.retry_at.isoformat(),
                            "latency_ms": result.latency_ms,
                            "failure_category": result.failure_category,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        }
                    )
                    if result.status != "verified":
                        rejected.append(
                            {
                                "provider": selection.profile.provider,
                                "model": selection.profile.model_id,
                                "capability": capability.value,
                                "status": result.status,
                                "failure": result.failure_category,
                            }
                        )
                        valid = False
                        break
                if valid:
                    accepted.append(selection)
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                task_id=task.id,
                correlation_id=task.id,
                event_type="DEV_MODEL_ROUTING",
                message="Dev Mode capability routing evaluated.",
                payload={
                    "required": sorted(c.value for c in capabilities),
                    "candidates": [s.profile.model_id for s in accepted],
                    "rejected": rejected,
                    "probes": probe_results,
                    "catalog": (
                        catalog_for(
                            self.settings.openrouter_base_url,
                            self.settings.openrouter_catalog_ttl_seconds,
                        ).status
                        if self.settings.openrouter_discovery_enabled
                        and any(p.provider == "openrouter" for p in profiles)
                        else None
                    ),
                    "provider": accepted[0].profile.provider if accepted else None,
                    "reason": accepted[0].reason if accepted else None,
                    "selected": accepted[0].profile.model_id if accepted else None,
                },
            )
            await self.session.commit()
            if not accepted:
                raise ProviderCallError(
                    "MODEL_CAPABILITY_UNAVAILABLE", "No bounded candidate passed behavioral probes"
                )
            bounded = accepted
        return bounded, capabilities

    async def _generate(
        self,
        run: AgentRun,
        request: ModelRequest,
        selections: list[ModelSelection],
        capabilities: frozenset[ModelCapability],
        task: Any,
        agent_role: str,
        validate_response: Callable[[ModelResponse], None] | None = None,
    ) -> ModelResponse:
        max_attempts = max(self.settings.model_transient_max_attempts, 1)
        final_error = ProviderCallError("MODEL_PROVIDER_UNAVAILABLE", "No model was available")
        previous: ModelSelection | None = None
        for selection_index, selection in enumerate(selections):
            provider = (
                self.provider
                if self.provider is not None
                else provider_for_profile(self.settings, selection.profile)
            )
            assert provider is not None
            try:
                self.policy.authorize(provider)
            except AgentRuntimeDomainError as exc:
                final_error = ProviderCallError(exc.code, exc.message, category="CONFIGURATION")
                continue
            if selection_index:
                await self._record_model_fallback(run.id, previous, selection, final_error)
            metadata = dict(request.metadata)
            if selection.profile.supported_parameters is not None:
                metadata["provider_supported_parameters"] = ",".join(
                    sorted(selection.profile.supported_parameters)
                )
            current_request = replace(request, model=selection.profile.model_id, metadata=metadata)
            for attempt in range(1, max_attempts + 1):
                try:
                    call = await self.economics.begin_call(
                        selection,
                        capabilities=frozenset(capability.value for capability in capabilities),
                        agent_role=agent_role,
                        task=task,
                        agent_run_id=run.id,
                        fallback_from=(
                            (previous.profile.provider, previous.profile.model_id)
                            if previous
                            else None
                        ),
                        fallback_reason=final_error.code if previous else None,
                    )
                except ProviderCallError as reservation_error:
                    # Economic/accounting refusals happen before a provider request, but an
                    # AgentRun already exists. Finalize the durable execution boundary here so
                    # no RUNNING AgentRun/TaskRun is stranded and never call the provider.
                    final_error = self._normalize_error(reservation_error)
                    await self._fail(run.id, final_error)
                    raise self._public_error(final_error) from None
                try:
                    async with asyncio.timeout(self.settings.model_request_timeout_seconds):
                        response = await provider.generate(current_request)
                    if validate_response is not None:
                        validate_response(response)
                except TimeoutError:
                    error = ProviderCallError(
                        "MODEL_TIMEOUT",
                        "The model request timed out",
                        retryable=True,
                        category="TIMEOUT",
                    )
                except ProviderCallError as provider_error:
                    error = self._normalize_error(provider_error)
                except Exception as exc:
                    logger.error(
                        "Unexpected agent runtime error",
                        extra={
                            "event": "agent_runtime_error",
                            "agent_run_id": str(run.id),
                            "task_id": str(run.task_id),
                            "error_type": type(exc).__name__,
                        },
                    )
                    error = ProviderCallError("AGENT_RUNTIME_ERROR", "Agent execution failed")
                else:
                    await self.economics.complete_call(call.id, response=response)
                    return response
                await self.economics.complete_call(call.id, error=error)
                final_error = error
                retryable = error.retryable or error.code in {
                    "MODEL_TIMEOUT",
                    "MODEL_RATE_LIMIT",
                    "MODEL_PROVIDER_TEMPORARY",
                    "MODEL_PROVIDER_ERROR",
                }
                if retryable and attempt < max_attempts:
                    await self._record_model_retry(run.id, error, attempt, max_attempts)
                    await asyncio.sleep(
                        self.settings.model_retry_base_seconds * (2 ** max(attempt - 1, 0))
                    )
                    continue
                break
            previous = selection
        await self._fail(run.id, final_error)
        raise self._public_error(final_error) from None

    def _validate_execution_truth_response(
        self,
        response: ModelResponse,
        context: ContextRecord,
        observations: list[ToolObservation],
    ) -> None:
        try:
            turn = self._parse_turn(response.output)
        except ValidationError as exc:
            raise ProviderCallError(
                "INVALID_MODEL_OUTPUT",
                "The model returned an invalid structured turn",
                category="RESPONSE_VALIDATION",
            ) from exc
        if not isinstance(turn, FinalTurn):
            return
        truth = ExecutionTruthValidator.validate(context, turn.result, observations)
        if not truth.accepted:
            raise ProviderCallError(
                truth.code,
                truth.message,
                category="TOOL_PROTOCOL",
            )

    async def _record_model_fallback(
        self,
        run_id: UUID,
        previous: ModelSelection | None,
        selected: ModelSelection,
        error: ProviderCallError,
    ) -> None:
        run = await self.agent_runs.get_for_update(run_id)
        if run is None or run.status != AgentRunStatus.RUNNING:
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Agent run was not found")
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "from_provider": previous.profile.provider if previous else run.provider,
            "from_model": previous.profile.model_id if previous else run.model_id,
            "to_provider": selected.profile.provider,
            "to_model": selected.profile.model_id,
            "reason": error.code,
        }
        run.fallback_history = [*run.fallback_history, entry][-10:]
        run.provider = selected.profile.provider
        run.model_alias = selected.profile.alias
        run.model_id = selected.profile.model_id
        run.economic_tier = selected.profile.tier.value
        run.selection_reason = selected.reason
        await self.session.commit()

    async def _record_model_retry(
        self,
        run_id: UUID,
        error: ProviderCallError,
        attempt: int,
        max_attempts: int,
    ) -> None:
        run = await self.agent_runs.get_for_update(run_id)
        if run is None or run.status != AgentRunStatus.RUNNING:
            raise AgentRuntimeDomainError(
                "AGENT_RUNTIME_ERROR", "Agent run disappeared during provider recovery"
            )
        history = list(run.request.get("retry_history", []))
        history.append(
            {
                "attempt": attempt,
                "at": datetime.now(UTC).isoformat(),
                "code": error.code,
                "message": error.message,
                "outcome": "RETRY_SCHEDULED",
            }
        )
        run.request = {**run.request, "retry_history": history[-10:]}
        task = await self.tasks.get_current(run.task_id)
        await self.events.create(
            company_id=task.company_id if task else None,
            project_id=task.project_id if task else None,
            agent_id=run.agent_id,
            task_id=run.task_id,
            event_type="AGENT_RUN_RETRY_SCHEDULED",
            message="Transient model-provider retry scheduled.",
            payload={
                "agent_run_id": str(run.id),
                "error_code": error.code,
                "attempt": attempt,
                "max_attempts": max_attempts,
            },
        )
        await self.session.commit()

    @staticmethod
    def _parse_turn(output: Any) -> FinalTurn | ToolRequestTurn:
        if isinstance(output, BaseAgentResult):
            return FinalTurn(type="final", result=output)
        if isinstance(output, dict) and output.get("status") == "completed":
            return FinalTurn(type="final", result=BaseAgentResult.model_validate(output))
        if isinstance(output, AgentTurnResponse):
            return output.turn
        if isinstance(output, FinalTurn | ToolRequestTurn):
            return output
        if isinstance(output, dict) and "turn" not in output:
            output = {"turn": output}
        return AgentTurnResponse.model_validate(output).turn

    async def _start_turn(
        self,
        task_id: UUID,
        selection: ModelSelection,
        capabilities: frozenset[ModelCapability],
        request: ModelRequest,
        *,
        first_turn: bool,
    ) -> AgentRun:
        if first_turn:
            task = await TaskStateMachine(self.session).transition(
                task_id,
                TaskStatus.IN_PROGRESS,
                "Agent runtime started execution",
                commit=False,
            )
            if task.status != TaskStatus.IN_PROGRESS:
                await self.session.commit()
                raise AgentRuntimeDomainError(
                    "AGENT_RUNTIME_ERROR", "Task could not enter execution", 409
                )
        else:
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                await self.session.rollback()
                raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task was not found")
            if task.status == TaskStatus.CANCELLED:
                await self.session.rollback()
                raise AgentRuntimeDomainError(
                    "AGENT_EXECUTION_CANCELLED", "Task was cancelled during agent execution", 409
                )
            if task.status != TaskStatus.IN_PROGRESS:
                await self.session.rollback()
                raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task is not in progress", 409)
        task_run = await self.task_runs.get_active_for_task(task.id)
        if task_run is None:
            await self.session.rollback()
            raise TaskRunStateConflictError("Agent execution has no active task run")
        now = datetime.now(UTC)
        run = await self.agent_runs.add(
            AgentRun(
                task_run_id=task_run.id,
                task_id=task.id,
                agent_id=task_run.agent_id,
                status=AgentRunStatus.RUNNING,
                provider=selection.profile.provider,
                model_alias=selection.profile.alias,
                model_id=selection.profile.model_id,
                economic_tier=selection.profile.tier.value,
                selection_reason=selection.reason,
                required_capabilities=sorted(capability.value for capability in capabilities),
                request=self._request_record(request),
                started_at=now,
            )
        )
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type="AGENT_RUN_STARTED",
            message="Agent model turn started.",
            payload={
                "agent_run_id": str(run.id),
                "task_run_id": str(task_run.id),
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
                "turn_number": request.metadata["turn_number"],
            },
        )
        await self.session.commit()
        logger.info(
            "Agent run started",
            extra={
                "event": "agent_run_started",
                "agent_run_id": str(run.id),
                "task_run_id": str(task_run.id),
                "task_id": str(task.id),
                "agent_id": str(task_run.agent_id),
                "status": run.status.value,
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
            },
        )
        return run

    async def _complete_turn(
        self,
        run_id: UUID,
        output: dict[str, Any],
        response: ModelResponse,
        *,
        final_result: BaseAgentResult | None,
        transition_to_review: bool,
    ) -> AgentRun:
        run = await self.agent_runs.get_for_update(run_id)
        if run is None or run.status != AgentRunStatus.RUNNING:
            await self.session.rollback()
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Agent run is not running")
        task = await self.tasks.get_current(run.task_id)
        if task is None:
            await self.session.rollback()
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task was not found")
        if task.status == TaskStatus.CANCELLED:
            await self.session.rollback()
            error = ProviderCallError(
                "AGENT_EXECUTION_CANCELLED", "Task was cancelled during agent execution"
            )
            await self._cancel_run(run_id, error)
            raise self._public_error(error)
        if task.status != TaskStatus.IN_PROGRESS:
            await self.session.rollback()
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task is not in progress")
        if final_result is not None and transition_to_review:
            task = await TaskStateMachine(self.session).transition(
                run.task_id,
                TaskStatus.REVIEW,
                "Agent runtime completed successfully",
                commit=False,
            )
        task_run = await self.task_runs.get(run.task_run_id)
        if task_run is None:
            await self.session.rollback()
            raise TaskRunStateConflictError("Agent run references a missing task run")
        usage = response.usage
        run.status = AgentRunStatus.SUCCEEDED
        run.response = output
        run.provider_response_id = response.response_id
        run.input_tokens = usage.input_tokens
        run.output_tokens = usage.output_tokens
        run.cached_tokens = usage.cached_input_tokens
        run.total_tokens = usage.total_tokens
        run.estimated_cost = self.costs.estimate(run.provider, run.model_id, usage)
        run.completed_at = datetime.now(UTC)
        if final_result is not None:
            task_run.output = output
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type="AGENT_RUN_SUCCEEDED",
            message="Agent model turn succeeded.",
            payload={
                "agent_run_id": str(run.id),
                "task_run_id": str(run.task_run_id),
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
                "turn_type": "final" if final_result is not None else "tool_call",
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "cached_tokens": run.cached_tokens,
                "total_tokens": run.total_tokens,
                "estimated_cost": (
                    str(run.estimated_cost) if run.estimated_cost is not None else None
                ),
            },
        )
        await self.session.commit()
        return run

    async def _fail(self, run_id: UUID, error: ProviderCallError) -> AgentRun:
        try:
            run = await self.agent_runs.get_for_update(run_id)
            if run is None:
                raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Agent run was not found")
            task = await self.tasks.get_for_update(run.task_id)
            if task is None:
                raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task was not found")
            if task.status == TaskStatus.IN_PROGRESS:
                task = await TaskStateMachine(self.session).transition(
                    run.task_id, TaskStatus.FAILED, error.message, commit=False
                )
            task_run = await self.task_runs.get(run.task_run_id)
            if task_run is None:
                raise TaskRunStateConflictError("Agent run references a missing task run")
            now = datetime.now(UTC)
            run.status = (
                AgentRunStatus.CANCELLED
                if task.status == TaskStatus.CANCELLED
                else AgentRunStatus.FAILED
            )
            run.error_code = error.code
            run.error_message = error.message
            run.completed_at = now
            if task_run.status in {TaskRunStatus.STARTED, TaskRunStatus.FAILED}:
                task_run.error = {"code": error.code, "message": error.message}
            await self._failed_event(run, task, error)
            await self.session.commit()
            self._log_failed(run, now)
            return run
        except Exception:
            await self.session.rollback()
            raise

    async def _fail_task(
        self,
        run: AgentRun,
        error: ProviderCallError,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        task = await self.tasks.get_for_update(run.task_id)
        if task is None:
            await self.session.rollback()
            return
        if task.status == TaskStatus.IN_PROGRESS:
            await TaskStateMachine(self.session).transition(
                task.id, TaskStatus.FAILED, error.message, commit=False
            )
        task_run = await self.task_runs.get(run.task_run_id)
        if task_run is not None:
            task_run.error = {
                "code": error.code,
                "message": error.message,
                **(details or {}),
            }
        await self.session.commit()

    @staticmethod
    def _safe_tool_arguments(turn: ToolRequestTurn) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in turn.arguments.items():
            if key == "content" and isinstance(value, str):
                safe[key] = f"[content omitted: {len(value)} characters]"
            elif key in {"path", "action", "target"}:
                safe[key] = value
        return safe

    async def _cancel_run(self, run_id: UUID, error: ProviderCallError) -> AgentRun:
        run = await self.agent_runs.get_for_update(run_id)
        if run is None:
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Agent run was not found")
        task = await self.tasks.get_current(run.task_id)
        if task is None:
            raise AgentRuntimeDomainError("AGENT_RUNTIME_ERROR", "Task was not found")
        if run.status == AgentRunStatus.RUNNING:
            run.status = AgentRunStatus.CANCELLED
            run.error_code = error.code
            run.error_message = error.message
            run.completed_at = datetime.now(UTC)
            await self._failed_event(run, task, error)
            await self.session.commit()
        return run

    async def _failed_event(self, run: AgentRun, task: Any, error: ProviderCallError) -> None:
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type="AGENT_RUN_FAILED",
            message="Agent model turn failed.",
            payload={
                "agent_run_id": str(run.id),
                "task_run_id": str(run.task_run_id),
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
                "error_code": error.code,
            },
        )

    def _request_record(self, request: ModelRequest) -> dict[str, Any]:
        common: dict[str, Any] = {
            "stored": self.settings.store_model_inputs,
            "metadata": request.metadata,
            "response_schema": request.response_model.__name__,
            "tool_names": [tool.name for tool in request.tools],
        }
        if self.settings.store_model_inputs:
            return {
                **common,
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
            }
        return {
            **common,
            "system_prompt_sha256": hashlib.sha256(
                request.system_prompt.encode("utf-8")
            ).hexdigest(),
            "user_prompt_sha256": hashlib.sha256(request.user_prompt.encode("utf-8")).hexdigest(),
        }

    def _log_succeeded(self, run: AgentRun, started: float, turn_type: str) -> None:
        logger.info(
            "Agent run succeeded",
            extra={
                "event": "agent_run_succeeded",
                "agent_run_id": str(run.id),
                "task_run_id": str(run.task_run_id),
                "task_id": str(run.task_id),
                "agent_id": str(run.agent_id),
                "status": run.status.value,
                "turn_type": turn_type,
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "cached_tokens": run.cached_tokens,
                "total_tokens": run.total_tokens,
                "estimated_cost": (
                    str(run.estimated_cost) if run.estimated_cost is not None else None
                ),
                "execution_duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )

    @staticmethod
    def _log_failed(run: AgentRun, now: datetime) -> None:
        logger.warning(
            "Agent run failed",
            extra={
                "event": "agent_run_failed",
                "agent_run_id": str(run.id),
                "task_id": str(run.task_id),
                "task_run_id": str(run.task_run_id),
                "agent_id": str(run.agent_id),
                "status": run.status.value,
                "provider": run.provider,
                "model_alias": run.model_alias,
                "model_id": run.model_id,
                "error_code": run.error_code,
                "duration_ms": (
                    round((now - run.started_at).total_seconds() * 1000, 2)
                    if run.started_at is not None
                    else None
                ),
            },
        )

    @staticmethod
    def _normalize_error(error: ProviderCallError) -> ProviderCallError:
        if error.code in NORMALIZED_PROVIDER_CODES:
            return error
        return ProviderCallError("AGENT_RUNTIME_ERROR", "Agent execution failed")

    @staticmethod
    def _public_error(error: ProviderCallError) -> AgentRuntimeDomainError:
        code = error.code if error.code in NORMALIZED_PROVIDER_CODES else "AGENT_RUNTIME_ERROR"
        status_code = {
            "MODEL_TIMEOUT": 504,
            "MODEL_AUTH_ERROR": 502,
            "MODEL_RATE_LIMIT": 429,
            "MODEL_QUOTA_EXHAUSTED": 402,
            "MODEL_CONFIGURATION_ERROR": 502,
            "MODEL_PROVIDER_TEMPORARY": 503,
            "MODEL_PROVIDER_ERROR": 502,
            "MODEL_CAPABILITY_UNAVAILABLE": 409,
            "PROVIDER_REQUEST_INVALID": 502,
            "PROVIDER_RESPONSE_INVALID": 502,
            "PROVIDER_TOOL_SCHEMA_INVALID": 502,
            "PROVIDER_TOOL_PROTOCOL_ERROR": 502,
            "UNVERIFIED_EXECUTION_CLAIM": 409,
            "INVALID_MODEL_OUTPUT": 502,
            "MAX_TOOL_STEPS_EXCEEDED": 409,
            "AGENT_EXECUTION_CANCELLED": 409,
            "MODEL_BUDGET_UNAVAILABLE": 409,
            "MODEL_CALL_BUDGET_EXHAUSTED": 409,
            "MISSION_MODEL_CALL_LIMIT_EXHAUSTED": 409,
            "FREE_MODEL_CALL_LIMIT_EXHAUSTED": 409,
            "PAID_MODEL_CALL_LIMIT_EXHAUSTED": 409,
            "MODEL_ACCOUNTING_STATE_LOST": 409,
            "MODEL_BUDGET_EXHAUSTED": 409,
        }.get(code, 500)
        return AgentRuntimeDomainError(code, error.message, status_code)
