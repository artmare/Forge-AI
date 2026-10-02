"""Typed, bounded contracts for Forge-owned completion state."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_MANIFEST_ENTRIES = 100
MAX_MANIFEST_SUMMARY = 500
PATH_CRITERION = re.compile(
    r"(?:`|\b)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+)(?:`|\b)"
)


def criterion_requires_judgment(criterion: str) -> bool:
    """Return false when an unverified criterion names a deterministic Forge gate."""
    lower = criterion.lower()
    if any(term in lower for term in ("test", "build", "lint", "typecheck", "type check")):
        return False
    return PATH_CRITERION.search(criterion) is None


class ManifestEvidenceStatus(StrEnum):
    VERIFIED = "VERIFIED"
    SUPPORTED = "SUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ArtifactProvenance(StrEnum):
    CURRENT_TASK_RUN = "CURRENT_TASK_RUN"
    RECOVERY_HISTORY = "RECOVERY_HISTORY"
    TASK_HISTORY = "TASK_HISTORY"
    WORKSPACE_DISCOVERY = "WORKSPACE_DISCOVERY"


class CompletionReadiness(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    REQUIRES_JUDGMENT = "REQUIRES_JUDGMENT"
    BLOCKED = "BLOCKED"


class ArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=4096)
    exists: bool
    sha256: str | None = None
    status: ManifestEvidenceStatus
    provenance: ArtifactProvenance
    originating_tool_call_id: UUID | None = None
    originating_agent_run_id: UUID | None = None
    originating_task_run_id: UUID | None = None
    checkpoint: str | None = Field(default=None, max_length=80)
    checkpoint_present: bool = False
    required: bool = False
    issue: str | None = Field(default=None, max_length=MAX_MANIFEST_SUMMARY)


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=64)
    reference: str = Field(max_length=512)
    status: ManifestEvidenceStatus
    execution_id: UUID | None = None
    tool_call_id: UUID | None = None
    agent_run_id: UUID | None = None
    task_run_id: UUID | None = None
    exit_code: int | None = None
    summary: str = Field(max_length=MAX_MANIFEST_SUMMARY)
    origin: str = Field(max_length=64)
    recorded_at: datetime | None = None


class AcceptanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_index: int = Field(ge=0)
    criterion: str = Field(max_length=1000)
    status: ManifestEvidenceStatus
    evidence_summary: str = Field(max_length=MAX_MANIFEST_SUMMARY)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    requires_judgment: bool = False


class CompletionManifest(BaseModel):
    """A bounded projection of facts Forge can prove without an LLM call."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    task_run_id: UUID
    company_id: UUID
    project_id: UUID
    workspace_identity: str = Field(max_length=200)
    checkpoint: str | None = Field(default=None, max_length=80)
    artifacts: list[ArtifactEvidence] = Field(
        default_factory=list, max_length=MAX_MANIFEST_ENTRIES
    )
    verification_results: list[VerificationEvidence] = Field(
        default_factory=list, max_length=MAX_MANIFEST_ENTRIES
    )
    acceptance_criteria: list[AcceptanceEvidence] = Field(
        default_factory=list, max_length=MAX_MANIFEST_ENTRIES
    )
    unresolved_failures: list[str] = Field(default_factory=list, max_length=50)
    blocking_reasons: list[str] = Field(default_factory=list, max_length=50)
    judgment_required: list[str] = Field(default_factory=list, max_length=50)
    readiness: CompletionReadiness
    human_review_required: bool = True
    generated_at: datetime

    def bounded_summary(self, *, include_evidence_ids: bool = True) -> dict[str, object]:
        artifacts = []
        for item in self.artifacts:
            value: dict[str, object] = {
                "path": item.path,
                "exists": item.exists,
                "sha256": item.sha256,
                "status": item.status.value,
                "provenance": item.provenance.value,
                "required": item.required,
                "checkpoint_present": item.checkpoint_present,
            }
            if include_evidence_ids:
                value["tool_call_id"] = (
                    str(item.originating_tool_call_id)
                    if item.originating_tool_call_id
                    else None
                )
            if item.issue:
                value["issue"] = item.issue
            artifacts.append(value)
        return {
            "task_id": str(self.task_id),
            "task_run_id": str(self.task_run_id),
            "workspace_identity": self.workspace_identity,
            "checkpoint": self.checkpoint,
            "readiness": self.readiness.value,
            "human_review_required": self.human_review_required,
            "artifacts": artifacts,
            "verification": [
                {
                    "kind": item.kind,
                    "reference": item.reference,
                    "status": item.status.value,
                    "summary": item.summary,
                    **(
                        {
                            "execution_id": str(item.execution_id)
                            if item.execution_id
                            else None,
                            "tool_call_id": str(item.tool_call_id)
                            if item.tool_call_id
                            else None,
                        }
                        if include_evidence_ids
                        else {}
                    ),
                }
                for item in self.verification_results
            ],
            "acceptance": [
                {
                    "criterion": item.criterion,
                    "status": item.status.value,
                    "requires_judgment": item.requires_judgment,
                    "evidence": item.evidence_summary,
                }
                for item in self.acceptance_criteria
            ],
            "blocking_reasons": self.blocking_reasons,
            "judgment_required": self.judgment_required,
            "unresolved_failures": self.unresolved_failures,
        }

    def model_summary(self) -> dict[str, object]:
        """Return only facts useful to the model; Forge retains provenance IDs internally."""
        value = self.bounded_summary(include_evidence_ids=False)
        value.pop("task_id", None)
        value.pop("task_run_id", None)
        value.pop("workspace_identity", None)
        return value
