from app.agent_runtime.contracts import ModelProvider
from app.core.config import Settings
from app.domain.exceptions import AgentRuntimeDomainError, PaidModelCallDisabledError


class ModelExecutionPolicy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authorize(self, provider: ModelProvider) -> None:
        if provider.paid and (
            not self.settings.allow_paid_model_calls
            or (self.settings.forge_dev_mode_enabled and self.settings.forge_dev_free_only)
        ):
            raise PaidModelCallDisabledError()
        enabled = {
            "mock": True,
            "openai": self.settings.openai_enabled,
            "gemini": self.settings.gemini_enabled,
            "openrouter": self.settings.openrouter_enabled,
        }.get(provider.name, False)
        if not enabled:
            raise AgentRuntimeDomainError(
                "MODEL_PROVIDER_DISABLED",
                f"The {provider.name} model provider is disabled",
                503,
            )
        if provider.name == "mock":
            return
        key = {
            "openai": self.settings.openai_api_key,
            "gemini": self.settings.gemini_api_key,
            "openrouter": self.settings.openrouter_api_key,
        }.get(provider.name)
        if key is None or not key.get_secret_value().strip():
            raise AgentRuntimeDomainError(
                "MODEL_AUTH_ERROR",
                f"{provider.name.title()} credentials are not configured",
                503,
            )
