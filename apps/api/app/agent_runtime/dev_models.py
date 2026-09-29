"""Worker-local TTL services; task accounting and durable events remain in AgentRuntime."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

from app.agent_runtime.contracts import ModelProvider, ModelRequest, ModelResponse
from app.agent_runtime.economics import ModelEconomicsService
from app.agent_runtime.openrouter_catalog import OpenRouterCatalogClient
from app.agent_runtime.probes import CapabilityProber
from app.agent_runtime.routing import EconomicTier, ModelProfile, ModelSelection
from app.domain.models import Task


@lru_cache(maxsize=8)
def catalog_for(base_url: str, ttl: int) -> OpenRouterCatalogClient:
    # The public catalog endpoint does not need credentials.
    return OpenRouterCatalogClient(base_url=base_url, api_key=None, ttl_seconds=ttl)


@lru_cache(maxsize=8)
def prober_for(base_url: str, ttl: int, retry: int, timeout: float) -> CapabilityProber:
    return CapabilityProber(ttl_seconds=ttl, retry_seconds=retry, timeout_seconds=timeout)


async def refresh_profiles(
    profiles: tuple[ModelProfile, ...], *, base_url: str, ttl: int, free_only: bool
) -> tuple[ModelProfile, ...]:
    catalog = await catalog_for(base_url, ttl).refresh()
    entries = {entry.model_id: entry for entry in catalog}
    result = []
    for profile in profiles:
        if profile.provider != "openrouter":
            result.append(profile)
            continue
        entry = entries.get(profile.model_id)
        if entry is None:
            # Unknown prices are never authority to spend in Dev Mode.
            result.append(replace(profile, enabled=False) if free_only else profile)
            continue
        result.append(
            replace(
                profile,
                paid=not entry.free,
                tier=EconomicTier.FREE if entry.free else EconomicTier.CHEAP,
                enabled=profile.enabled and (entry.free or not free_only),
                supported_parameters=entry.supported_parameters,
                context_length=entry.context_length,
            )
        )
    return tuple(result)


class AccountedProbeProvider:
    """Record every external probe call in the existing task budget ledger."""

    def __init__(
        self,
        provider: ModelProvider,
        economics: ModelEconomicsService,
        selection: ModelSelection,
        task: Task,
    ) -> None:
        self.provider, self.economics = provider, economics
        self.selection, self.task = selection, task
        self.name, self.paid = provider.name, provider.paid

    async def generate(self, request: ModelRequest) -> ModelResponse:
        from app.agent_runtime.contracts import ProviderCallError

        call = await self.economics.begin_call(
            self.selection,
            capabilities=frozenset({"CAPABILITY_PROBE"}),
            agent_role="CAPABILITY_PROBE",
            task=self.task,
        )
        try:
            response = await self.provider.generate(request)
        except ProviderCallError as error:
            await self.economics.complete_call(call.id, error=error)
            raise
        except BaseException:
            await self.economics.complete_call(
                call.id,
                error=ProviderCallError(
                    "MODEL_TIMEOUT",
                    "Probe interrupted",
                    retryable=True,
                ),
            )
            raise
        await self.economics.complete_call(call.id, response=response)
        return response
