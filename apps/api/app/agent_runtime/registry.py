from dataclasses import dataclass

from app.agent_runtime.routing import EconomicTier, ModelCapability, ModelProfile
from app.core.config import Settings
from app.domain.exceptions import ModelAliasNotFoundError


@dataclass(frozen=True)
class ResolvedModel:
    alias: str
    model_id: str
    provider: str = "mock"
    tier: EconomicTier = EconomicTier.FREE
    capabilities: frozenset[ModelCapability] = frozenset(ModelCapability)
    enabled: bool = True
    paid: bool = False
    max_call_cost: float | None = None


class ModelRegistry:
    def __init__(
        self,
        aliases: dict[str, str],
        profiles: list[ModelProfile] | None = None,
    ) -> None:
        self._aliases = {key.strip().lower(): value.strip() for key, value in aliases.items()}
        self._profiles = {profile.alias: profile for profile in (profiles or [])}

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRegistry":
        profiles: list[ModelProfile] = []
        for raw in settings.model_catalog:
            alias = str(raw.get("alias", "")).strip().lower()
            provider = str(raw.get("provider", "")).strip().lower()
            model_id = str(raw.get("model", raw.get("model_id", ""))).strip()
            if not alias or not provider or not model_id:
                continue
            capabilities = frozenset(
                ModelCapability(str(value).strip().upper())
                for value in raw.get("capabilities", ["TEXT"])
            )
            profiles.append(
                ModelProfile(
                    alias=alias,
                    provider=provider,
                    model_id=model_id,
                    tier=EconomicTier(str(raw.get("tier", "FREE")).strip().upper()),
                    capabilities=capabilities,
                    enabled=bool(raw.get("enabled", True)),
                    paid=bool(raw.get("paid", False)),
                    max_call_cost=(
                        float(raw["max_call_cost"])
                        if raw.get("max_call_cost") is not None
                        else None
                    ),
                    supported_parameters=(
                        frozenset(str(value) for value in raw["supported_parameters"])
                        if isinstance(raw.get("supported_parameters"), list)
                        else None
                    ),
                )
            )
        if not profiles:
            provider = settings.model_provider.strip().lower()
            paid = provider != "mock"
            tier = EconomicTier.PREMIUM if paid else EconomicTier.FREE
            profiles = [
                ModelProfile(
                    alias=alias,
                    provider=provider,
                    model_id=model_id,
                    tier=tier,
                    capabilities=frozenset(ModelCapability),
                    enabled=True,
                    paid=paid,
                    max_call_cost=(0.05 if paid else 0),
                )
                for alias, model_id in settings.model_aliases.items()
            ]
        return cls(settings.model_aliases, profiles)

    def resolve(self, alias: str) -> ResolvedModel:
        normalized = alias.strip().lower()
        profile = self._profiles.get(normalized)
        if profile is not None:
            return ResolvedModel(
                alias=profile.alias,
                model_id=profile.model_id,
                provider=profile.provider,
                tier=profile.tier,
                capabilities=profile.capabilities,
                enabled=profile.enabled,
                paid=profile.paid,
                max_call_cost=profile.max_call_cost,
            )
        model_id = self._aliases.get(normalized)
        if not model_id:
            raise ModelAliasNotFoundError(alias)
        return ResolvedModel(alias=normalized, model_id=model_id)

    @property
    def profiles(self) -> tuple[ModelProfile, ...]:
        return tuple(self._profiles.values())

    def add_discovered_profiles(
        self, profiles: list[ModelProfile], *, free_only: bool = True
    ) -> None:
        """Merge discovery results without overriding operator configuration."""
        for profile in profiles:
            if free_only and profile.tier != EconomicTier.FREE:
                continue
            self._profiles.setdefault(profile.alias, profile)
