from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Forge API"
    app_env: str = "development"
    app_version: str = "0.9.0"
    database_url: str = "postgresql+asyncpg://localhost/forge"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"
    event_source: str = "forge-api"
    event_stream_name: str = "forge.events"
    event_dlq_stream_name: str = "forge.events.dlq"
    event_publish_batch_size: int = 50
    event_publish_interval_ms: int = 500
    event_processing_lease_seconds: int = 30
    event_max_retries: int = 5
    event_retry_base_seconds: float = 1.0
    event_consumer_block_ms: int = 1_000
    event_stream_maxlen: int = 10_000
    event_publisher_healthy_seconds: int = 10
    model_provider: str = "mock"
    allow_paid_model_calls: bool = False
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    openai_enabled: bool = True
    gemini_enabled: bool = False
    openrouter_enabled: bool = False
    openrouter_discovery_enabled: bool = True
    openrouter_catalog_ttl_seconds: int = 3600
    openrouter_free_only: bool = True
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_catalog: list[dict[str, Any]] = Field(default_factory=list)
    model_request_timeout_seconds: float = 120.0
    model_transient_max_attempts: int = 3
    model_retry_base_seconds: float = 0.25
    model_provider_health_cooldown_seconds: int = 300
    model_fallback_max_candidates: int = 3
    store_model_inputs: bool = False
    agent_run_stale_seconds: int = 300
    tools_enabled: bool = True
    filesystem_list_enabled: bool = True
    filesystem_read_enabled: bool = True
    filesystem_write_enabled: bool = True
    shell_run_enabled: bool = False
    tool_workspace_root: str = "/workspaces"
    filesystem_tool_timeout_seconds: float = 10.0
    tool_file_read_max_bytes: int = 262_144
    tool_file_write_max_bytes: int = 262_144
    tool_observation_max_chars: int = 50_000
    agent_max_tool_steps: int = 10
    tool_call_stale_seconds: int = 300
    autonomy_enabled: bool = False
    orchestrator_reconcile_interval_seconds: float = 5.0
    orchestrator_recovery_interval_seconds: float = 10.0
    worker_poll_interval_ms: int = 500
    worker_heartbeat_interval_seconds: float = 10.0
    worker_stale_seconds: int = 30
    agent_worker_concurrency: int = 2
    forge_max_concurrent_tasks: int = 4
    job_lease_seconds: int = 60
    job_lease_renew_interval_seconds: float = 20.0
    job_retry_base_seconds: float = 5.0
    execution_job_max_attempts: int = 3
    worker_shutdown_grace_seconds: float = 30.0
    model_default: str = "gpt-5.4-mini"
    model_fast: str = "gpt-5.4-nano"
    model_reasoning: str = "gpt-5.4"
    model_coding: str = "gpt-5.4-mini"
    model_planner: str = "gpt-5.4-mini"
    model_developer_fast: str = "gpt-5.4-nano"
    model_developer_coding: str = "gpt-5.4-mini"
    model_developer_reasoning: str = "gpt-5.4"
    model_qa_fast: str = "gpt-5.4-nano"
    model_qa_visual: str = "gpt-5.4-mini"
    model_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    max_model_calls_per_task: int = 24
    max_model_calls_per_mission: int = 100
    max_free_model_calls_per_task: int = 24
    max_paid_model_calls_per_task: int = 8
    # A task-wide cap remains conservative at 24 calls; the per-iteration cap
    # matches it so bounded multi-step development turns are not cut off before
    # deterministic validation can run. Deployments may lower this explicitly.
    max_model_calls_per_iteration: int = 24
    max_input_tokens_per_task: int = 240_000
    max_output_tokens_per_task: int = 48_000
    max_estimated_cost_per_task: float | None = None
    max_ai_spend_per_task: float | None = None
    max_ai_spend_per_mission: float | None = None
    model_budget_warning_ratio: float = 0.8
    context_recent_observations: int = 3
    duplicate_tool_turn_threshold: int = 2
    product_qa_enabled: bool = True
    mission_max_agents: int = 8
    mission_max_tasks: int = 30
    mission_max_dependencies: int = 100
    mission_max_planning_attempts: int = 3
    planning_run_stale_seconds: int = 300
    planner_request_timeout_seconds: float = 30.0
    planner_provider_max_attempts: int = 3
    planner_retry_base_seconds: float = 0.5
    development_enabled: bool = True
    forge_dev_mode_enabled: bool = False
    forge_self_development_enabled: bool = False
    forge_dev_context_checkpoint_ratio: float = 0.7
    forge_dev_specialist_limit: int = 2
    development_runner_mode: str = "queue"
    development_runner_queue_root: str = "/runner-queue"
    development_runner_poll_interval_ms: int = 100
    development_output_max_bytes: int = 50_000
    development_execution_stale_seconds: int = 900
    development_max_executions: int = 8
    development_bootstrap_enabled: bool = True
    development_bootstrap_max_attempts: int = 3
    developer_max_steps: int = 20
    developer_max_fix_attempts: int = 3
    development_project_lease_seconds: int = 900
    development_test_timeout_seconds: int = 300
    development_build_timeout_seconds: int = 300
    development_lint_timeout_seconds: int = 120
    development_install_timeout_seconds: int = 600
    git_tool_timeout_seconds: int = 30

    @field_validator(
        "max_estimated_cost_per_task",
        "max_ai_spend_per_task",
        "max_ai_spend_per_mission",
        mode="before",
    )
    @classmethod
    def empty_optional_number_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def model_aliases(self) -> dict[str, str]:
        return {
            "default": self.model_default,
            "fast": self.model_fast,
            "reasoning": self.model_reasoning,
            "coding": self.model_coding,
            "planner": self.model_planner,
            "developer_fast": self.model_developer_fast,
            "developer_coding": self.model_developer_coding,
            "developer_reasoning": self.model_developer_reasoning,
            "qa_fast": self.model_qa_fast,
            "qa_visual": self.model_qa_visual,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
