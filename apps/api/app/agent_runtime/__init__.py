"""Controlled Phase 05 model execution boundary.

Keep the package initializer import-light: tool contracts depend on the provider
contracts, while the runtime depends on tool contracts.  The lazy compatibility
export avoids making either import order authoritative for standalone workers.
"""

from typing import Any

__all__ = ["AgentRuntime"]


def __getattr__(name: str) -> Any:
    if name == "AgentRuntime":
        from app.agent_runtime.runtime import AgentRuntime

        return AgentRuntime
    raise AttributeError(name)
