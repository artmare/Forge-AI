from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.agent_runtime.contracts import ProviderCallError


class ModelCapability(StrEnum):
    TEXT = "TEXT"
    CODING = "CODING"
    REASONING = "REASONING"
    TOOL_CALLING = "TOOL_CALLING"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    VISION = "VISION"


class EconomicTier(StrEnum):
    FREE = "FREE"
    CHEAP = "CHEAP"
    PREMIUM = "PREMIUM"


class ProviderHealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    INVALID_TOOL_CALL = "INVALID_TOOL_CALL"
    STRUCTURED_OUTPUT_FAILURE = "STRUCTURED_OUTPUT_FAILURE"
    TIMEOUT = "TIMEOUT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


@dataclass(frozen=True)
class ModelProfile:
    alias: str
    provider: str
    model_id: str
    tier: EconomicTier
    capabilities: frozenset[ModelCapability]
    enabled: bool = True
    paid: bool = False
    max_call_cost: float | None = None
    supported_parameters: frozenset[str] | None = None
    context_length: int | None = None

    def supports(self, required: frozenset[ModelCapability]) -> bool:
        return required.issubset(self.capabilities)


@dataclass(frozen=True)
class RoutingContext:
    required_capabilities: frozenset[ModelCapability]
    paid_budget_remaining: float
    requested_alias: str | None = None
    requested_quality: EconomicTier = EconomicTier.FREE
    escalation_reason: str | None = None
    provider_health: dict[tuple[str, str], ProviderHealthStatus] | None = None
    excluded_profiles: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class ModelSelection:
    profile: ModelProfile
    reason: str


class ModelRouter:
    """Deterministic FREE -> CHEAP -> PREMIUM capability and budget router."""

    JUSTIFIED_ESCALATIONS = frozenset(
        {
            "CAPABILITY_UNAVAILABLE",
            "REPEATED_DETERMINISTIC_FAILURE",
            "INVALID_STRUCTURED_OUTPUT",
            "BOUNDED_IMPLEMENTATION_FAILURE",
            "ARCHITECTURE_AMBIGUITY",
            "PROVIDER_FAILOVER",
        }
    )

    def __init__(self, profiles: Iterable[ModelProfile]) -> None:
        self.profiles = tuple(profiles)

    def candidates(self, context: RoutingContext) -> list[ModelSelection]:
        health = context.provider_health or {}
        compatible: list[ModelProfile] = []
        saw_capability_match = False
        saw_paid_candidate = False
        for profile in self.profiles:
            if (
                not profile.enabled
                or (profile.provider, profile.model_id) in context.excluded_profiles
            ):
                continue
            if not profile.supports(context.required_capabilities):
                continue
            saw_capability_match = True
            state = health.get((profile.provider, profile.model_id), ProviderHealthStatus.HEALTHY)
            if state != ProviderHealthStatus.HEALTHY:
                continue
            if profile.paid:
                saw_paid_candidate = True
                reserve = profile.max_call_cost
                if context.paid_budget_remaining <= 0:
                    continue
                if reserve is None or reserve > context.paid_budget_remaining:
                    continue
            compatible.append(profile)

        if not compatible:
            if saw_paid_candidate and context.paid_budget_remaining <= 0:
                raise ProviderCallError(
                    "MODEL_BUDGET_UNAVAILABLE",
                    "No compatible free model is available and paid AI budget is unavailable",
                    category="BUDGET",
                )
            if saw_capability_match:
                raise ProviderCallError(
                    "MODEL_PROVIDER_UNAVAILABLE",
                    "Compatible models are disabled, unhealthy, or outside the configured budget",
                    category="PROVIDER_HEALTH",
                )
            raise ProviderCallError(
                "MODEL_CAPABILITY_UNAVAILABLE",
                "No enabled model satisfies the required capabilities",
                category="CAPABILITY",
            )

        order = {EconomicTier.FREE: 0, EconomicTier.CHEAP: 1, EconomicTier.PREMIUM: 2}
        requested = (context.requested_alias or "").strip().lower()
        compatible.sort(
            key=lambda profile: (
                order[profile.tier],
                0 if profile.alias == requested else 1,
                profile.alias,
            )
        )
        selections: list[ModelSelection] = []
        lower_tier_available = any(
            profile.tier in {EconomicTier.FREE, EconomicTier.CHEAP} for profile in compatible
        )
        for profile in compatible:
            if profile.tier == EconomicTier.PREMIUM:
                escalation_reason = context.escalation_reason
                if escalation_reason is None and not lower_tier_available:
                    escalation_reason = "CAPABILITY_UNAVAILABLE"
                if escalation_reason not in self.JUSTIFIED_ESCALATIONS:
                    continue
                reason = f"PREMIUM selected after justified escalation: {escalation_reason}."
            elif profile.tier == EconomicTier.CHEAP:
                reason = (
                    "CHEAP selected because no compatible healthy FREE model remained and "
                    "paid budget allowed the configured reservation."
                )
            else:
                reason = (
                    "FREE selected as the cheapest compatible healthy model under the "
                    "free-first policy."
                )
            selections.append(ModelSelection(profile=profile, reason=reason))
        if selections:
            return selections
        raise ProviderCallError(
            "MODEL_ESCALATION_REQUIRED",
            "Only PREMIUM models are compatible and no justified escalation was supplied",
            category="POLICY",
        )

    def select(self, context: RoutingContext) -> ModelSelection:
        return self.candidates(context)[0]


def required_capabilities(
    *, role: str, has_tools: bool, structured_output: bool = True, vision: bool = False
) -> frozenset[ModelCapability]:
    values = {ModelCapability.TEXT}
    normalized = role.strip().upper()
    if normalized in {"DEVELOPER", "LEAD_ENGINEER", "LEAD ENGINEER", "QA", "CODE_REVIEWER"}:
        values.add(ModelCapability.CODING)
    if normalized in {"PLANNER", "CEO", "ARCHITECT", "LEAD_ENGINEER", "LEAD ENGINEER"}:
        values.add(ModelCapability.REASONING)
    if has_tools:
        values.add(ModelCapability.TOOL_CALLING)
    if structured_output:
        values.add(ModelCapability.STRUCTURED_OUTPUT)
    if vision:
        values.add(ModelCapability.VISION)
    return frozenset(values)
