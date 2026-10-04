"""Bounded Forge-owned preconditions for development completion."""

from __future__ import annotations

import asyncio
import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.development.completion_contracts import PATH_CRITERION
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager

MAX_REQUIRED_DELIVERABLES = 100


class DeliverableCompletionStatus(StrEnum):
    VERIFIED_CURRENT = "VERIFIED_CURRENT"
    VERIFIED_RECOVERY = "VERIFIED_RECOVERY"
    MISSING = "MISSING"
    UNSAFE = "UNSAFE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALIDATED = "INVALIDATED"


class DeliverableCompletionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    status: DeliverableCompletionStatus
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    reason: str | None = Field(default=None, max_length=300)

    @property
    def complete(self) -> bool:
        return self.status in {
            DeliverableCompletionStatus.VERIFIED_CURRENT,
            DeliverableCompletionStatus.VERIFIED_RECOVERY,
        }


class CompletionSafetySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_deliverables: list[str] = Field(
        default_factory=list, max_length=MAX_REQUIRED_DELIVERABLES
    )
    deliverable_states: list[DeliverableCompletionState] = Field(
        default_factory=list, max_length=MAX_REQUIRED_DELIVERABLES
    )

    @property
    def ready(self) -> bool:
        return all(item.complete for item in self.deliverable_states)

    @property
    def incomplete_deliverables(self) -> list[str]:
        return [item.path for item in self.deliverable_states if not item.complete]

    def handoff_summary(self) -> dict[str, Any]:
        return {
            "completion_safe": self.ready,
            "required_deliverables": self.required_deliverables,
            "incomplete_deliverables": self.incomplete_deliverables,
            "deliverable_states": [
                item.model_dump(mode="json") for item in self.deliverable_states
            ],
        }


def required_deliverables(
    task_input: dict[str, Any] | None, acceptance_criteria: list[Any] | None
) -> list[str]:
    """Derive bounded deliverables from task-owned inputs and explicit criterion paths."""
    raw = task_input.get("deliverables", []) if isinstance(task_input, dict) else []
    values = [item for item in raw if isinstance(item, str) and 0 < len(item) <= 4096]
    for criterion in acceptance_criteria or []:
        values.extend(match.group(1) for match in PATH_CRITERION.finditer(str(criterion)))
    return sorted(dict.fromkeys(values))[:MAX_REQUIRED_DELIVERABLES]


def criterion_paths(criterion: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in PATH_CRITERION.finditer(criterion)))


def criterion_requires_static_verification(criterion: str) -> bool:
    lower = criterion.lower()
    return any(term in lower for term in ("link", "reference", "local asset"))


class DevelopmentCompletionSafety:
    """Verify required artifacts against current workspace state and trusted mutation scope."""

    def __init__(self, settings: Settings) -> None:
        self.workspaces = WorkspaceManager(settings.tool_workspace_root)

    async def evaluate(
        self,
        *,
        company_id: UUID,
        project_id: UUID | None,
        task_input: dict[str, Any] | None,
        acceptance_criteria: list[Any] | None,
        current_mutations: set[str],
        recovery_hashes: dict[str, str],
    ) -> CompletionSafetySnapshot:
        required = required_deliverables(task_input, acceptance_criteria)
        if not required:
            return CompletionSafetySnapshot()
        if project_id is None:
            return CompletionSafetySnapshot(
                required_deliverables=required,
                deliverable_states=[
                    DeliverableCompletionState(
                        path=path,
                        status=DeliverableCompletionStatus.UNSAFE,
                        reason="Task has no authorized project workspace.",
                    )
                    for path in required
                ],
            )
        states = await asyncio.gather(
            *(
                asyncio.to_thread(
                    self._inspect,
                    company_id,
                    project_id,
                    path,
                    path in current_mutations,
                    recovery_hashes.get(path),
                )
                for path in required
            )
        )
        return CompletionSafetySnapshot(
            required_deliverables=required,
            deliverable_states=list(states),
        )

    def _inspect(
        self,
        company_id: UUID,
        project_id: UUID,
        path: str,
        current_mutation: bool,
        recovery_hash: str | None,
    ) -> DeliverableCompletionState:
        try:
            workspace = self.workspaces.existing_project_workspace(company_id, project_id)
            if workspace is None:
                raise ToolSystemError("FILE_NOT_FOUND", "Project workspace does not exist")
            _, candidate = self.workspaces.resolve(
                company_id, project_id, path, must_exist=True
            )
            self.workspaces.ensure_regular_file(candidate)
            digest = self._sha256(candidate)
        except ToolSystemError as exc:
            missing = exc.code == "FILE_NOT_FOUND"
            return DeliverableCompletionState(
                path=path,
                status=(
                    DeliverableCompletionStatus.MISSING
                    if missing
                    else DeliverableCompletionStatus.UNSAFE
                ),
                reason=(
                    "Required deliverable is missing."
                    if missing
                    else "Required deliverable is not a safe regular workspace file."
                ),
            )
        except OSError:
            return DeliverableCompletionState(
                path=path,
                status=DeliverableCompletionStatus.UNSAFE,
                reason="Required deliverable could not be inspected safely.",
            )
        if current_mutation:
            return DeliverableCompletionState(
                path=path,
                status=DeliverableCompletionStatus.VERIFIED_CURRENT,
                sha256=digest,
            )
        if recovery_hash is not None:
            if digest == recovery_hash:
                return DeliverableCompletionState(
                    path=path,
                    status=DeliverableCompletionStatus.VERIFIED_RECOVERY,
                    sha256=digest,
                )
            return DeliverableCompletionState(
                path=path,
                status=DeliverableCompletionStatus.INVALIDATED,
                sha256=digest,
                reason="Current content differs from checkpoint-verified recovery evidence.",
            )
        return DeliverableCompletionState(
            path=path,
            status=DeliverableCompletionStatus.UNSUPPORTED,
            sha256=digest,
            reason="File exists without accepted current-run or recovery mutation evidence.",
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(131072), b""):
                digest.update(chunk)
        return digest.hexdigest()
