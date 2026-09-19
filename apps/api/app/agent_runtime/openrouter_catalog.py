from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.agent_runtime.contracts import ProviderCallError
from app.agent_runtime.routing import EconomicTier, ModelCapability, ModelProfile


@dataclass(frozen=True)
class OpenRouterCatalogEntry:
    model_id: str
    name: str
    context_length: int | None
    capabilities: frozenset[ModelCapability]
    free: bool
    supported_parameters: frozenset[str]

    def profile(self) -> ModelProfile:
        return ModelProfile(
            alias=f"openrouter:{self.model_id}",
            provider="openrouter",
            model_id=self.model_id,
            tier=EconomicTier.FREE if self.free else EconomicTier.CHEAP,
            capabilities=self.capabilities,
            paid=not self.free,
            max_call_cost=0 if self.free else None,
            supported_parameters=self.supported_parameters,
        )


class OpenRouterCatalogClient:
    """Bounded, TTL-cached OpenRouter catalog discovery with no credential logging."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout_seconds: float = 15,
        ttl_seconds: int = 3600,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/", timeout=timeout_seconds, headers=headers
        )
        self.ttl = timedelta(seconds=max(ttl_seconds, 1))
        self._cached_at: datetime | None = None
        self._cache: tuple[OpenRouterCatalogEntry, ...] = ()

    async def discover(self, *, force: bool = False) -> tuple[OpenRouterCatalogEntry, ...]:
        now = datetime.now(UTC)
        if not force and self._cached_at is not None and now - self._cached_at < self.ttl:
            return self._cache
        try:
            response = await self.client.get("models")
        except httpx.TimeoutException as exc:
            raise ProviderCallError(
                "MODEL_TIMEOUT", "OpenRouter model discovery timed out", retryable=True,
                category="TIMEOUT",
            ) from exc
        except httpx.NetworkError as exc:
            raise ProviderCallError(
                "MODEL_PROVIDER_TEMPORARY",
                "OpenRouter model discovery is temporarily unavailable",
                retryable=True,
                category="NETWORK",
            ) from exc
        if response.status_code >= 400:
            raise ProviderCallError(
                (
                    "MODEL_PROVIDER_TEMPORARY"
                    if response.status_code >= 500
                    else "MODEL_CONFIGURATION_ERROR"
                ),
                "OpenRouter rejected model discovery",
                retryable=response.status_code >= 500,
                category="DISCOVERY",
                http_status=response.status_code,
            )
        try:
            rows = response.json()["data"]
            entries = tuple(
                entry for row in rows if isinstance(row, dict) if (entry := self._parse(row))
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderCallError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenRouter returned an invalid model catalog",
                category="RESPONSE_VALIDATION",
            ) from exc
        self._cache = entries
        self._cached_at = now
        return entries

    @staticmethod
    def _parse(row: dict[str, Any]) -> OpenRouterCatalogEntry | None:
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            return None
        parameters = frozenset(
            str(value) for value in row.get("supported_parameters", []) if isinstance(value, str)
        )
        capabilities = {ModelCapability.TEXT}
        if "tools" in parameters or "tool_choice" in parameters:
            capabilities.add(ModelCapability.TOOL_CALLING)
        if "response_format" in parameters or "structured_outputs" in parameters:
            capabilities.add(ModelCapability.STRUCTURED_OUTPUT)
        architecture = row.get("architecture")
        modalities = (
            architecture.get("input_modalities", [])
            if isinstance(architecture, dict)
            else []
        )
        if "image" in modalities:
            capabilities.add(ModelCapability.VISION)
        # OpenRouter does not provide authoritative coding/reasoning flags. Preserve
        # explicit metadata when present; otherwise probes/configuration must add them.
        declared = row.get("forge_capabilities", [])
        for value in declared if isinstance(declared, list) else []:
            try:
                capabilities.add(ModelCapability(str(value).upper()))
            except ValueError:
                continue
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        free = OpenRouterCatalogClient._zero(
            pricing.get("prompt")
        ) and OpenRouterCatalogClient._zero(pricing.get("completion"))
        context_length = row.get("context_length")
        return OpenRouterCatalogEntry(
            model_id=model_id,
            name=str(row.get("name") or model_id),
            context_length=context_length if isinstance(context_length, int) else None,
            capabilities=frozenset(capabilities),
            free=free,
            supported_parameters=parameters,
        )

    @staticmethod
    def _zero(value: Any) -> bool:
        try:
            return Decimal(str(value)) == 0
        except (InvalidOperation, TypeError, ValueError):
            return False
