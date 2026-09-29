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

    def needed(self, request: ModelRequest, limit: int) -> bool:
        # Conservative byte bound includes native exchange state and tool schemas.
        size = len((request.system_prompt + request.user_prompt).encode())
        size += len((request.conversation_start_prompt or "").encode())
        for exchange in request.tool_exchanges:
            size += len(json.dumps(exchange.response).encode())
            size += len(json.dumps(exchange.call.arguments).encode())
        size += sum(
            len(json.dumps(t.input_model.model_json_schema()).encode()) for t in request.tools
        )
        return size >= limit * self.ratio

    def checkpoint(
        self,
        *,
        task_id: str,
        goal: str,
        observations: list[ToolObservation],
        tool_steps: int,
        duplicate_signals: int,
    ) -> dict[str, Any]:
        if self.count >= self.maximum:
            raise ValueError("Context rollover limit exhausted")
        self.count += 1
        results = []
        failures = []
        inspected, modified, tests = [], [], []
        diff = ""
        for item in observations:
            result = item.result or {}
            reference = result.get("path") or result.get("action")
            record = {
                "tool": item.tool,
                "status": item.status,
                "reference": reference,
                "execution_status": result.get("status"),
            }
            results.append(record)
            if item.status != "success" or result.get("status", "SUCCEEDED") != "SUCCEEDED":
                failures.append(
                    {**record, "error": item.error.model_dump() if item.error else None}
                )
            if item.tool == "filesystem.read":
                inspected.append(reference)
            if item.tool in {"filesystem.write", "filesystem.patch"} and item.status == "success":
                modified.append(reference)
            if item.tool == "development.execute":
                tests.append(record)
            if item.tool == "git.diff":
                diff = str(result.get("stdout_excerpt", result.get("stdout", "")))[:4000]
        self.handoff = {
            "task_id": task_id,
            "goal": goal[:2000],
            "phase": "EXECUTING",
            "rollover_count": self.count,
            "tool_steps": tool_steps,
            "duplicate_signals": duplicate_signals,
            "completed": results[-30:],
            "files_inspected": inspected[-30:],
            "files_modified": modified[-30:],
            "current_diff": diff,
            "test_status": tests[-20:],
            "failures": failures[-20:],
            "decisions": [],
            "open_questions": [],
            "next_action": "Continue this task. Inspect state; do not replay mutations. "
            "Resolve recorded failures before claiming success.",
        }
        return self.handoff
