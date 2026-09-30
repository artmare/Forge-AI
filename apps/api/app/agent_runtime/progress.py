"""Authoritative development progress and conservative observation reuse."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.tool_system.contracts import (
    ToolObservation,
    ToolObservationProvenance,
    ToolRequestTurn,
    canonical_git_reference,
)

READ_ONLY_TOOLS = frozenset(
    {"filesystem.list", "filesystem.read", "git.status", "git.diff", "git.log"}
)
MUTATING_TOOLS = frozenset(
    {
        "filesystem.write",
        "filesystem.patch",
        "development.execute",
        "development.install_dependencies",
        "git.init",
        "git.commit",
    }
)


def tool_signature(tool: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"tool": tool, "arguments": arguments},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def result_fingerprint(observation: ToolObservation) -> str:
    return hashlib.sha256(
        json.dumps(
            observation.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class CachedObservation:
    signature: str
    generation: int
    observation: ToolObservation
    result_hash: str


@dataclass
class DevelopmentProgress:
    """Runtime-local state rebuilt from durable calls when an execution resumes.

    Reuse is deliberately limited to deterministic read-only tools and is invalidated by
    every Forge-controlled operation that may change workspace or Git state.
    """

    generation: int = 0
    cache: dict[str, CachedObservation] = field(default_factory=dict)
    reused_observations: int = 0
    stale_invalidations: int = 0
    stagnation_signals: int = 0
    last_useful_action: dict[str, Any] = field(default_factory=dict)
    progress_since_rollover: list[dict[str, Any]] = field(default_factory=list)

    def reusable(self, turn: ToolRequestTurn) -> ToolObservation | None:
        if turn.tool_name not in READ_ONLY_TOOLS:
            return None
        signature = tool_signature(turn.tool_name, dict(turn.arguments))
        cached = self.cache.get(signature)
        if cached is None or cached.generation != self.generation:
            return None
        self.reused_observations += 1
        self.stagnation_signals += 1
        source = cached.observation
        compact = self._compact_reuse(source)
        compact["observation_reused"] = True
        compact["reused_from_tool_call_id"] = str(source.tool_call_id)
        compact["authoritative_result_sha256"] = cached.result_hash
        compact["workspace_generation"] = self.generation
        git_reference = canonical_git_reference(source.tool)
        if git_reference is not None:
            compact["action"] = git_reference
        return ToolObservation(
            tool_call_id=source.tool_call_id,
            tool=source.tool,
            status=source.status,
            result=compact,
            error=source.error,
            truncated=True,
            original_chars=source.original_chars,
            provenance=ToolObservationProvenance.REUSED_TOOL_CALL_RESULT,
            task_id=source.task_id,
            task_run_id=source.task_run_id,
            agent_run_id=source.agent_run_id,
        )

    def record(self, turn: ToolRequestTurn, observation: ToolObservation) -> int:
        """Record one actual Forge execution and return invalidated cache entries."""
        invalidated = 0
        validation_only_failure = (
            observation.status == "error"
            and observation.error is not None
            and observation.error.code == "TOOL_ARGUMENT_VALIDATION_FAILED"
        )
        if turn.tool_name in MUTATING_TOOLS and not validation_only_failure:
            invalidated = len(self.cache)
            self.cache.clear()
            self.generation += 1
            self.stale_invalidations += invalidated
        if observation.status == "success" and turn.tool_name in READ_ONLY_TOOLS:
            signature = tool_signature(turn.tool_name, dict(turn.arguments))
            self.cache[signature] = CachedObservation(
                signature=signature,
                generation=self.generation,
                observation=observation,
                result_hash=result_fingerprint(observation),
            )
            self.stagnation_signals = 0
        elif self._is_useful(turn, observation):
            reference = (observation.result or {}).get("path") or (observation.result or {}).get(
                "action"
            )
            useful = {
                "tool": turn.tool_name,
                "status": observation.status,
                "reference": reference,
            }
            self.last_useful_action = useful
            self.progress_since_rollover.append(useful)
            self.progress_since_rollover = self.progress_since_rollover[-20:]
            self.stagnation_signals = 0
        return invalidated

    def record_reuse(self, observation: ToolObservation) -> dict[str, Any]:
        result = observation.result or {}
        return {
            "tool": observation.tool,
            "reference": (
                canonical_git_reference(observation.tool)
                or result.get("path")
                or result.get("action")
            ),
            "provenance": observation.provenance.value,
            "source_tool_call_id": str(observation.tool_call_id),
            "result_sha256": result.get("authoritative_result_sha256"),
            "workspace_generation": self.generation,
            "stagnation_signals": self.stagnation_signals,
        }

    def rollover_recorded(self) -> list[dict[str, Any]]:
        progress = list(self.progress_since_rollover)
        self.progress_since_rollover.clear()
        return progress

    @staticmethod
    def _is_useful(turn: ToolRequestTurn, observation: ToolObservation) -> bool:
        if turn.tool_name in MUTATING_TOOLS:
            return True
        return observation.status != "success"

    @staticmethod
    def _compact_reuse(observation: ToolObservation) -> dict[str, Any]:
        result = dict(observation.result or {})
        content = result.pop("content", None)
        if isinstance(content, str):
            result["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
            result["content_original_chars"] = len(content)
            result["safe_summary"] = (
                f"{result.get('path', 'file')}: {content.count(chr(10)) + 1} lines, "
                f"{len(content.encode())} bytes"
            )
        for key in ("stdout_excerpt", "stderr_excerpt", "preview"):
            value = result.pop(key, None)
            if isinstance(value, str):
                result[f"{key}_sha256"] = hashlib.sha256(value.encode()).hexdigest()
                result[f"{key}_chars"] = len(value)
                if len(value) <= 1000:
                    result[key] = value
        entries = result.get("entries")
        if isinstance(entries, list) and len(entries) > 100:
            result["entries"] = entries[:100]
            result["entries_truncated"] = True
        return result
