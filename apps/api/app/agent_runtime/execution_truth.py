from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.agent_runtime.builders import ContextRecord
from app.agent_runtime.contracts import BaseAgentResult
from app.tool_system.contracts import ToolObservation


class EvidenceKind(StrEnum):
    FILE_MUTATION = "FILE_MUTATION"
    COMMAND = "COMMAND"
    TEST = "TEST"
    GIT = "GIT"
    BROWSER = "BROWSER"


class EvidenceProvenance(StrEnum):
    CURRENT_RUN = "CURRENT_RUN"
    RECOVERY_HISTORY = "RECOVERY_HISTORY"


@dataclass(frozen=True)
class ExecutionEvidence:
    kind: EvidenceKind
    tool: str
    reference: str | None = None
    provenance: EvidenceProvenance = EvidenceProvenance.CURRENT_RUN
    task_id: UUID | None = None
    project_id: UUID | None = None
    agent_run_id: UUID | None = None
    tool_call_id: UUID | None = None
    artifact_sha256: str | None = None
    checkpoint: str | None = None
    executed_at: datetime | None = None
    verified_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionTruthDecision:
    accepted: bool
    code: str
    message: str
    evidence: tuple[ExecutionEvidence, ...] = ()
    failed_claim: dict[str, Any] | None = None


class ExecutionTruthValidator:
    """Validate completion against Forge-owned observations, never model prose.

    Execution evidence is required by the claims in a completion, not by the agent's
    role. Artifact and structured execution claims are checked structurally; a small
    set of high-confidence prose patterns protects providers that return an execution
    claim in the summary instead of the structured fields.
    """

    _MUTATION_TOOLS = frozenset(
        {
            "filesystem.write",
            "filesystem.patch",
        }
    )
    _COMMAND_TOOLS = frozenset(
        {"shell.run", "development.execute", "development.install_dependencies"}
    )
    _TEST_ACTIONS = frozenset(
        {
            "NODE_TEST",
            "NODE_BUILD",
            "NODE_LINT",
            "NODE_TYPECHECK",
            "PYTHON_TEST",
            "PYTHON_LINT",
        }
    )
    _TEST_SUCCESS_CLAIM = re.compile(
        r"\b(?:(?:all\s+)?(?:tests?|test suite|lint|typecheck|build)(?:\s+\w+){0,3}\s+"
        r"(?:passed|succeeded|green)|(?:passed|successful)\s+(?:all\s+)?tests?)\b",
        re.IGNORECASE,
    )
    _FILE_MUTATION_CLAIM = re.compile(
        r"\b(?:wrote|created|modified|edited|patched|deleted|removed|renamed|moved|saved)\b"
        r"[^\n]{0,120}(?:\b(?:file|directory|folder|artifact)\b|"
        r"(?:^|[\s'\"`/])[-\w./]+\.[a-zA-Z0-9]{1,12}\b)",
        re.IGNORECASE,
    )
    _GIT_CLAIM = re.compile(
        r"\b(?:committed|pushed|merged|rebased|cherry-picked)\b(?:[^\n]{0,80}\bgit\b)?",
        re.IGNORECASE,
    )
    _BROWSER_CLAIM = re.compile(
        r"\b(?:page|interface|UI) (?:looks|renders|rendered) (?:correct|good|properly)|"
        r"\b(?:captured|took) (?:a )?screenshot\b",
        re.IGNORECASE,
    )
    _COMMAND_CLAIM = re.compile(
        r"\b(?:ran|executed)\b[^\n]{0,80}\b(?:command|script|shell|migration)\b|"
        r"\bdeploy(?:ed|ment succeeded)\b",
        re.IGNORECASE,
    )

    @classmethod
    def validate(
        cls,
        context: ContextRecord,
        result: BaseAgentResult,
        observations: list[ToolObservation],
        *,
        historical_evidence: tuple[ExecutionEvidence, ...] = (),
        current_task_id: UUID | None = None,
        current_project_id: UUID | None = None,
        current_agent_run_id: UUID | None = None,
    ) -> ExecutionTruthDecision:
        current_evidence = cls.collect(
            observations,
            task_id=current_task_id,
            project_id=current_project_id,
            agent_run_id=current_agent_run_id,
        )
        trusted_history = tuple(
            item
            for item in historical_evidence
            if (current_task_id is None or item.task_id == current_task_id)
            and (current_project_id is None or item.project_id == current_project_id)
        )
        evidence = (*current_evidence, *trusted_history)
        artifacts = result.output.get("artifacts", [])
        if not isinstance(artifacts, list):
            return ExecutionTruthDecision(
                False,
                "UNVERIFIED_EXECUTION_CLAIM",
                "Completion artifacts must be a list backed by Forge execution evidence.",
                evidence,
            )
        mutation_paths = {
            item.reference
            for item in evidence
            if item.kind == EvidenceKind.FILE_MUTATION and item.reference is not None
        }
        unsupported = sorted(
            {
                str(path)
                for path in artifacts
                if isinstance(path, str) and path not in mutation_paths
            }
        )
        if unsupported:
            return ExecutionTruthDecision(
                False,
                "UNVERIFIED_EXECUTION_CLAIM",
                "Completion declared artifacts without successful Forge mutation evidence: "
                + ", ".join(unsupported[:10]),
                evidence,
            )
        claims = result.output.get("execution_claims", [])
        if not isinstance(claims, list):
            return ExecutionTruthDecision(
                False,
                "UNVERIFIED_EXECUTION_CLAIM",
                "Structured execution_claims must be a list.",
                evidence,
            )
        evidence_kinds = {item.kind.value for item in evidence}
        for claim in claims:
            if not isinstance(claim, dict):
                return ExecutionTruthDecision(
                    False,
                    "UNVERIFIED_EXECUTION_CLAIM",
                    "A structured execution claim has no matching Forge evidence.",
                    evidence,
                )
            claim_kind = claim.get("kind")
            claim_reference = claim.get("reference")
            claim_scope = str(claim.get("scope") or "CURRENT_RUN").upper()
            claim_tool_call_id = claim.get("tool_call_id")
            matching_evidence = [item for item in evidence if item.kind.value == claim_kind]
            expected_provenance = (
                EvidenceProvenance.RECOVERY_HISTORY
                if claim_scope == "HISTORICAL"
                else EvidenceProvenance.CURRENT_RUN
            )
            matching_evidence = [
                item for item in matching_evidence if item.provenance == expected_provenance
            ]
            if isinstance(claim_reference, str):
                matching_evidence = [
                    item for item in matching_evidence if item.reference == claim_reference
                ]
            if isinstance(claim_tool_call_id, str):
                matching_evidence = [
                    item
                    for item in matching_evidence
                    if str(item.tool_call_id) == claim_tool_call_id
                ]
            if not matching_evidence:
                return ExecutionTruthDecision(
                    False,
                    "UNVERIFIED_EXECUTION_CLAIM",
                    "A structured execution claim has no matching Forge evidence.",
                    evidence,
                    {
                        "kind": claim_kind,
                        "reference": claim_reference,
                        "scope": claim_scope,
                        "tool_call_id": claim_tool_call_id,
                    },
                )

        prose = "\n".join(
            [result.summary]
            + [str(item) for item in result.output.get("details", []) if isinstance(item, str)]
            + result.notes
        )
        current_kinds = {item.kind.value for item in current_evidence}
        historical_qualifier = re.search(
            r"\b(?:previous|earlier|historical|recovery checkpoint|prior execution)\b",
            prose,
            re.IGNORECASE,
        )
        if cls._TEST_SUCCESS_CLAIM.search(prose) and EvidenceKind.TEST.value not in current_kinds:
            return ExecutionTruthDecision(
                False,
                "UNVERIFIED_EXECUTION_CLAIM",
                "The completion claims validation passed without a successful Forge test result.",
                evidence,
            )
        prose_requirements = (
            (cls._FILE_MUTATION_CLAIM, EvidenceKind.FILE_MUTATION, "filesystem mutation"),
            (cls._GIT_CLAIM, EvidenceKind.GIT, "Git action"),
            (cls._COMMAND_CLAIM, EvidenceKind.COMMAND, "command execution"),
            (cls._BROWSER_CLAIM, EvidenceKind.BROWSER, "browser verification"),
        )
        for pattern, kind, description in prose_requirements:
            if not pattern.search(prose):
                continue
            allowed_kinds = evidence_kinds if historical_qualifier else current_kinds
            if kind.value not in allowed_kinds:
                return ExecutionTruthDecision(
                    False,
                    "UNVERIFIED_EXECUTION_CLAIM",
                    f"The completion claims {description} without matching Forge evidence.",
                    evidence,
                )
        return ExecutionTruthDecision(
            True,
            "EXECUTION_EVIDENCE_VERIFIED",
            "Completion is consistent with Forge execution evidence.",
            evidence,
        )

    @classmethod
    def collect(
        cls,
        observations: list[ToolObservation],
        *,
        task_id: UUID | None = None,
        project_id: UUID | None = None,
        agent_run_id: UUID | None = None,
    ) -> tuple[ExecutionEvidence, ...]:
        collected: list[ExecutionEvidence] = []
        for observation in observations:
            if observation.status != "success":
                continue
            result: dict[str, Any] = observation.result or {}
            if observation.tool in cls._MUTATION_TOOLS:
                path = result.get("path")
                collected.append(
                    ExecutionEvidence(
                        EvidenceKind.FILE_MUTATION,
                        observation.tool,
                        str(path) if isinstance(path, str) else None,
                        task_id=task_id,
                        project_id=project_id,
                        agent_run_id=agent_run_id,
                        tool_call_id=observation.tool_call_id,
                    )
                )
            elif observation.tool in cls._COMMAND_TOOLS:
                action = result.get("action")
                execution_succeeded = str(result.get("status", "SUCCEEDED")).upper() == "SUCCEEDED"
                # A later failed run supersedes an earlier pass of the same action.
                collected = [
                    item
                    for item in collected
                    if not (item.tool == observation.tool and item.reference == str(action))
                ]
                if not execution_succeeded:
                    continue
                kind = EvidenceKind.TEST if action in cls._TEST_ACTIONS else EvidenceKind.COMMAND
                collected.append(
                    ExecutionEvidence(
                        kind,
                        observation.tool,
                        str(action) if action is not None else None,
                        task_id=task_id,
                        project_id=project_id,
                        agent_run_id=agent_run_id,
                        tool_call_id=observation.tool_call_id,
                    )
                )
            elif observation.tool.startswith("git."):
                if str(result.get("status", "SUCCEEDED")).upper() == "SUCCEEDED":
                    collected.append(
                        ExecutionEvidence(
                            EvidenceKind.GIT,
                            observation.tool,
                            task_id=task_id,
                            project_id=project_id,
                            agent_run_id=agent_run_id,
                            tool_call_id=observation.tool_call_id,
                        )
                    )
            elif observation.tool == "browser.capture" and (
                result.get("rendered") is True and result.get("artifact") and result.get("sha256")
            ):
                collected.append(
                    ExecutionEvidence(
                        EvidenceKind.BROWSER,
                        observation.tool,
                        str(result["artifact"]),
                        task_id=task_id,
                        project_id=project_id,
                        agent_run_id=agent_run_id,
                        tool_call_id=observation.tool_call_id,
                    )
                )
        return tuple(collected)
