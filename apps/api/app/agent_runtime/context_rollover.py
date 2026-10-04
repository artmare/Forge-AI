"""Compact handoffs derived from Forge observations, not claims from model prose."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.agent_runtime.contracts import ModelRequest
from app.tool_system.contracts import ToolObservation


def mutation_key(name: str, arguments: dict[str, Any]) -> str | None:
    if name not in {"filesystem.write", "filesystem.patch", "git.commit"}:
        return None
    return hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()


@dataclass
class ContextRollover:
    ratio: float
    maximum: int
    count: int = 0
    handoff: dict[str, Any] = field(default_factory=dict)
    executed_mutations: set[str] = field(default_factory=set)
    budget_soft_checkpointed: bool = False
    last_rollover_tool_steps: int = 0
    causes: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def estimated_input_tokens(request: ModelRequest, *, reserve_tokens: int = 0) -> int:
        """Conservative provider-neutral estimate including native continuation state."""
        prompt = (
            request.conversation_start_prompt or request.user_prompt
            if request.tool_exchanges
            else request.user_prompt
        )
        size = len((request.system_prompt + prompt).encode())
        for exchange in request.tool_exchanges:
            size += len(json.dumps(exchange.response).encode())
            size += len(json.dumps(exchange.call.arguments).encode())
        size += sum(
            len(json.dumps(t.input_model.model_json_schema()).encode()) for t in request.tools
        )
        if request.metadata.get("structured_output_schema_sent") == "true":
            size += len(json.dumps(request.response_model.model_json_schema()).encode())
        # UTF-8 bytes / 3 is intentionally conservative for code and JSON.
        return (size + 2) // 3 + max(reserve_tokens, 0)

    def needed(self, request: ModelRequest, limit: int, *, reserve_tokens: int = 0) -> bool:
        return (
            self.estimated_input_tokens(request, reserve_tokens=reserve_tokens)
            >= limit * self.ratio
        )

    @staticmethod
    def input_breakdown(request: ModelRequest) -> dict[str, int]:
        """Approximate provider-visible bytes without retaining prompt contents."""
        prompt = (
            request.conversation_start_prompt or request.user_prompt
            if request.tool_exchanges
            else request.user_prompt
        )
        breakdown = {
            "system_runtime": len(request.system_prompt.encode()),
            "conversation_prompt": len(prompt.encode()),
            "tool_schemas": sum(
                len(json.dumps(tool.input_model.model_json_schema()).encode())
                for tool in request.tools
            ),
            "structured_output_schema": (
                len(json.dumps(request.response_model.model_json_schema()).encode())
                if request.metadata.get("structured_output_schema_sent") == "true"
                else 0
            ),
            "tool_call_arguments": sum(
                len(json.dumps(exchange.call.arguments).encode())
                for exchange in request.tool_exchanges
            ),
            "tool_observations": sum(
                len(json.dumps(exchange.response).encode()) for exchange in request.tool_exchanges
            ),
        }
        raw_components = request.metadata.get("context_component_bytes")
        if raw_components:
            try:
                components = json.loads(raw_components)
            except (TypeError, json.JSONDecodeError):
                components = {}
            if isinstance(components, dict):
                for key, value in components.items():
                    if isinstance(value, int) and value >= 0:
                        breakdown[f"prompt_{key}"] = value
        return breakdown

    def checkpoint(
        self,
        *,
        task_id: str,
        goal: str,
        observations: list[ToolObservation],
        tool_steps: int,
        duplicate_signals: int,
        reason: str = "MODEL_CONTEXT_THRESHOLD",
        file_states: list[dict[str, Any]] | None = None,
        required_actions: list[str] | None = None,
        completion_safety: dict[str, Any] | None = None,
        remaining_budget: dict[str, int] | None = None,
        progress: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self.count >= self.maximum:
            raise ValueError("Context rollover limit exhausted")
        self.count += 1
        results = []
        failure_candidates: list[tuple[int, dict[str, Any]]] = []
        inspected, modified, tests = [], [], []
        latest_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        git_state: dict[str, Any] = {}
        for observation_index, item in enumerate(observations):
            result = item.result or {}
            reference = result.get("path") or result.get("action")
            record = {
                "tool": item.tool,
                "status": item.status,
                "reference": reference,
                "execution_status": result.get("status"),
            }
            identity = (item.tool, str(reference or ""))
            if item.status == "success":
                latest_by_identity[identity] = record
            if item.status != "success" or result.get("status", "SUCCEEDED") != "SUCCEEDED":
                failure_candidates.append(
                    (
                        observation_index,
                        {**record, "error": item.error.model_dump() if item.error else None},
                    )
                )
            if item.tool == "filesystem.read":
                inspected.append(reference)
            if item.tool in {"filesystem.write", "filesystem.patch"} and item.status == "success":
                modified.append(reference)
            if item.tool == "development.execute" and result.get("action"):
                tests.append(record)
            if item.tool in {"git.status", "git.diff"} and item.status == "success":
                output = str(result.get("stdout_excerpt", result.get("stdout", "")))
                git_state[item.tool] = {
                    "status": result.get("status"),
                    "output_chars": len(output),
                    "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                    "preview": output[:1000],
                }
        results = list(latest_by_identity.values())[-20:]
        failures = []
        for failure_index, failure in failure_candidates:
            error = failure.get("error") or {}
            details = error.get("details") or {}
            resolving_tools = {failure["tool"]}
            suggested_tool = details.get("suggested_tool")
            if isinstance(suggested_tool, str):
                resolving_tools.add(suggested_tool)
            resolved = any(
                item.status == "success"
                and item.tool in resolving_tools
                and (
                    failure["reference"] is None
                    or ((item.result or {}).get("path") or (item.result or {}).get("action"))
                    == failure["reference"]
                    or item.tool == suggested_tool
                )
                for item in observations[failure_index + 1 :]
            )
            if not resolved:
                failures.append(failure)
        completed_actions = {
            str((item.result or {}).get("action"))
            for item in observations
            if item.tool == "development.execute"
            and item.status == "success"
            and str((item.result or {}).get("status", "")).upper() == "SUCCEEDED"
        }
        missing_actions = sorted(set(required_actions or []) - completed_actions)
        safety = completion_safety or {
            "completion_safe": True,
            "required_deliverables": [],
            "incomplete_deliverables": [],
            "deliverable_states": [],
        }
        incomplete_deliverables = [
            str(item) for item in safety.get("incomplete_deliverables", []) if item
        ][:30]
        successful_tools = {item.tool for item in observations if item.status == "success"}
        unresolved = failures[-1] if failures else None
        if unresolved and (unresolved.get("error") or {}).get("code") == (
            "TOOL_ARGUMENT_VALIDATION_FAILED"
        ):
            error = unresolved.get("error") or {}
            details = error.get("details") or {}
            next_action = (
                f"Repair only the malformed {unresolved['tool']} request using its declared "
                f"schema. Required fields: {details.get('required_fields', [])}; "
                f"invalid fields: {details.get('invalid_fields', [])}."
            )
        elif unresolved:
            next_action = (
                f"Resolve the recorded {unresolved['tool']} failure before any further inspection."
            )
        elif incomplete_deliverables:
            next_action = (
                "Create or repair the next incomplete required deliverable with an authorized "
                f"mutation tool: {incomplete_deliverables[0]}. Incomplete deliverables: "
                + ", ".join(incomplete_deliverables)
                + ". Do not finalize until Forge records successful evidence for each one."
            )
        elif missing_actions:
            next_action = (
                "Run the next required deterministic validation action with development.execute: "
                + missing_actions[0]
                + "."
            )
        elif "git.status" not in successful_tools:
            next_action = (
                "Inspect git.status once, then continue directly to remaining verification."
            )
        elif "git.diff" not in successful_tools:
            next_action = "Inspect git.diff once, then return the verified final result."
        else:
            next_action = (
                "The required repository observations are already authoritative. Return the final "
                "structured result now; do not reread or rewrite unchanged files."
            )
        self.handoff = {
            "task_id": task_id,
            "goal": goal[:2000],
            "phase": "EXECUTING",
            "rollover_count": self.count,
            "tool_steps": tool_steps,
            "duplicate_signals": duplicate_signals,
            "rollover_reason": reason,
            "completed": results,
            "successful_tool_counts": {
                name: sum(item.tool == name and item.status == "success" for item in observations)
                for name in sorted(successful_tools)
            },
            "files_inspected": sorted({str(item) for item in inspected if item}),
            "files_modified": sorted({str(item) for item in modified if item}),
            "file_states": (file_states or [])[-30:],
            "git_state": git_state,
            "test_status": tests[-20:],
            "failures": failures[-20:],
            "required_validation_actions": sorted(required_actions or []),
            "remaining_validation_actions": missing_actions,
            "verification_still_required": bool(missing_actions or incomplete_deliverables),
            "continuation_mode": (
                "EXECUTION_REPAIR" if incomplete_deliverables else "VERIFICATION_FINALIZATION"
            ),
            "completion_safety": safety,
            "mutation_fingerprints": sorted(self.executed_mutations),
            "progress_since_last_rollover": (progress or [])[-20:],
            "remaining_budget": remaining_budget or {},
            "decisions": [],
            "open_questions": [],
            "next_action": next_action,
        }
        self.last_rollover_tool_steps = tool_steps
        if reason == "TASK_BUDGET_SOFT_THRESHOLD":
            self.budget_soft_checkpointed = True
        return self.handoff
