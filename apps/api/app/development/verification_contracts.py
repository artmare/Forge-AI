"""Forge-owned verification planning and bounded visual-judgment evidence."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_VERIFICATION_STEPS = 32
MAX_VERIFICATION_SUMMARY = 500


class VerificationKind(StrEnum):
    ARTIFACTS = "ARTIFACTS"
    STATIC_WEB = "STATIC_WEB"
    NODE_LINT = "NODE_LINT"
    NODE_TYPECHECK = "NODE_TYPECHECK"
    NODE_TEST = "NODE_TEST"
    NODE_BUILD = "NODE_BUILD"
    PYTHON_LINT = "PYTHON_LINT"
    PYTHON_TEST = "PYTHON_TEST"
    GIT_STATUS = "GIT_STATUS"
    BROWSER_DESKTOP = "BROWSER_DESKTOP"
    BROWSER_MOBILE = "BROWSER_MOBILE"
    VISUAL_QA = "VISUAL_QA"


class VerificationStepStatus(StrEnum):
    PLANNED = "PLANNED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    STALE = "STALE"


class VisualQADecision(StrEnum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    REJECT = "REJECT"


class VisualFindingSeverity(StrEnum):
    BLOCKING = "BLOCKING"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    SUGGESTION = "SUGGESTION"


class VerificationStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=80)
    kind: VerificationKind
    reason: str = Field(max_length=MAX_VERIFICATION_SUMMARY)
    criterion_indices: list[int] = Field(default_factory=list, max_length=50)
    deliverables: list[str] = Field(default_factory=list, max_length=50)
    deterministic: bool = True
    blocking: bool = True
    mechanism: str = Field(max_length=80)
    dependencies: list[str] = Field(default_factory=list, max_length=16)
    expected_evidence: str = Field(max_length=200)


class VerificationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    task_run_id: UUID
    company_id: UUID
    project_id: UUID
    workspace_identity: str = Field(max_length=200)
    version: int = Field(ge=1, le=100)
    source_generation: str = Field(min_length=64, max_length=64)
    profile: str = Field(max_length=40)
    required_deliverables: list[str] = Field(default_factory=list, max_length=100)
    steps: list[VerificationStep] = Field(default_factory=list, max_length=MAX_VERIFICATION_STEPS)
    judgment_requirements: list[str] = Field(default_factory=list, max_length=50)
    reconciliation_reasons: list[str] = Field(default_factory=list, max_length=20)
    generated_at: datetime

    def model_projection(self) -> dict[str, object]:
        """Return the small part useful during implementation, without internal IDs."""
        return {
            "required_deliverables": self.required_deliverables,
            "required_verification": [
                {
                    "kind": step.kind.value,
                    "blocking": step.blocking,
                    "reason": step.reason,
                }
                for step in self.steps
                if step.blocking
            ],
            "judgment_requirements": self.judgment_requirements,
        }

    def bounded_summary(self) -> dict[str, object]:
        return {
            "version": self.version,
            "source_generation": self.source_generation,
            "profile": self.profile,
            "required_deliverables": self.required_deliverables,
            "steps": [step.model_dump(mode="json") for step in self.steps],
            "judgment_requirements": self.judgment_requirements,
            "reconciliation_reasons": self.reconciliation_reasons,
        }


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_key: str = Field(max_length=80)
    kind: VerificationKind
    status: VerificationStepStatus
    source_generation: str = Field(min_length=64, max_length=64)
    input_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    summary: str = Field(max_length=MAX_VERIFICATION_SUMMARY)
    execution_id: UUID | None = None
    tool_call_id: UUID | None = None
    exit_code: int | None = None
    evidence_reference: str | None = Field(default=None, max_length=512)
    reused: bool = False
    skipped_reason: str | None = Field(default=None, max_length=MAX_VERIFICATION_SUMMARY)
    recorded_at: datetime


class VisualQACaptureReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: UUID
    artifact: str = Field(max_length=512)
    sha256: str = Field(min_length=64, max_length=64)
    width: int = Field(ge=320, le=1920)
    height: int = Field(ge=240, le=1440)


class VisualQAFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(max_length=80)
    severity: VisualFindingSeverity
    finding: str = Field(max_length=500)
    recommendation: str = Field(max_length=500)
    criterion_indices: list[int] = Field(default_factory=list, max_length=20)


class VisualQAResult(BaseModel):
    """The model-authored judgment portion; it cannot assert capture provenance."""

    model_config = ConfigDict(extra="forbid")

    decision: VisualQADecision
    findings: list[VisualQAFinding] = Field(default_factory=list, max_length=20)
    summary: str = Field(max_length=1000)


class VisualQAEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    task_run_id: UUID
    source_generation: str = Field(min_length=64, max_length=64)
    captures: list[VisualQACaptureReference] = Field(min_length=1, max_length=4)
    reviewer_provider: str = Field(max_length=64)
    reviewer_model: str = Field(max_length=200)
    decision: VisualQADecision
    findings: list[VisualQAFinding] = Field(default_factory=list, max_length=20)
    criteria_addressed: list[int] = Field(default_factory=list, max_length=50)
    summary: str = Field(max_length=1000)
    recorded_at: datetime
    supersedes_event_id: UUID | None = None
