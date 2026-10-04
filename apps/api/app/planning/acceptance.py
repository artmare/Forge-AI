"""Deterministic classification of Mission acceptance criteria."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class AcceptanceMechanism(StrEnum):
    ARTIFACT = "ARTIFACT"
    COMMAND = "COMMAND"
    BEHAVIOR = "BEHAVIOR"
    VERIFICATION_EVIDENCE = "VERIFICATION_EVIDENCE"
    BROWSER = "BROWSER"


@dataclass(frozen=True)
class AcceptanceCriterionAnalysis:
    mechanisms: frozenset[AcceptanceMechanism]
    browser_required: bool = False
    requires_judgment: bool = False

    @property
    def observable(self) -> bool:
        return bool(self.mechanisms)


_PATH = re.compile(
    r"(?:`|\b)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+)(?:`|\b)"
)
_COMMAND = re.compile(
    r"(?:\b(?:test|tests|build|lint|typecheck|type\s+check|pytest|ruff|mypy)\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?[A-Za-z0-9:_-]+\b)",
    re.IGNORECASE,
)
_COMMAND_OUTCOME = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|fail(?:s|ed|ing)?|succeed(?:s|ed|ing)?|"
    r"exit(?:s|ed|ing)?|result|output|error(?:s)?|status)\b",
    re.IGNORECASE,
)
_OBSERVABLE_BEHAVIOR = re.compile(
    r"\b(?:contain(?:s|ed|ing)?|display(?:s|ed|ing)?|render(?:s|ed|ing)?|"
    r"respond(?:s|ed|ing)?|return(?:s|ed|ing)?|support(?:s|ed|ing)?|"
    r"provide(?:s|d|ing)?|produce(?:s|d|ing)?|create(?:s|d|ing)?|"
    r"reject(?:s|ed|ing)?|prevent(?:s|ed|ing)?|implement(?:s|ed|ing)?|"
    r"resolve(?:s|d|ing)?|wrap(?:s|ped|ping)?|load(?:s|ed|ing)?|"
    r"(?:is|are)\s+(?:present|visible|readable|associated|linked|available)|"
    r"no\s+(?:unintended\s+)?(?:overflow|errors?|broken\s+links?))\b",
    re.IGNORECASE,
)
_VERIFICATION_ACTION = re.compile(
    r"\b(?:verif(?:y|ies|ied|ying|ication)|validat(?:e|es|ed|ing|ion)|"
    r"record(?:s|ed|ing)?|document(?:s|ed|ing)?|capture(?:s|d|ing)?|"
    r"report(?:s|ed|ing)?|inspect(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
_EVIDENCE = re.compile(
    r"\b(?:evidence|report|results?|outputs?|logs?|artifacts?|findings?)\b",
    re.IGNORECASE,
)
_VIEWPORT = re.compile(r"\b(?:viewport|viewports|browser|screenshot|rendered\s+page)\b", re.I)
_VIEWPORT_SIZE = re.compile(r"\b\d{3,4}\s*[x×]\s*\d{3,4}\b", re.IGNORECASE)
_BROWSER_RUNTIME = re.compile(
    r"\b(?:console\s+errors?|horizontal\s+overflow|screenshot|browser\s+capture)\b",
    re.IGNORECASE,
)
_NEGATED_REQUIREMENT = re.compile(r"\bno\b", re.IGNORECASE)
_SUBJECTIVE = re.compile(
    r"\b(?:looks?\s+good|beautiful|attractive|polished|professional|"
    r"visually\s+appealing|visual\s+quality|nice)\b",
    re.IGNORECASE,
)


def analyze_acceptance_criterion(criterion: str) -> AcceptanceCriterionAnalysis:
    """Classify observable mechanisms without treating evidence prose as evidence itself."""
    mechanisms: set[AcceptanceMechanism] = set()
    has_path = _PATH.search(criterion) is not None
    has_command = _COMMAND.search(criterion) is not None
    has_command_outcome = _COMMAND_OUTCOME.search(criterion) is not None
    has_behavior = _OBSERVABLE_BEHAVIOR.search(criterion) is not None
    has_verification_action = _VERIFICATION_ACTION.search(criterion) is not None
    has_evidence = _EVIDENCE.search(criterion) is not None
    has_viewport = _VIEWPORT.search(criterion) is not None
    has_viewport_size = _VIEWPORT_SIZE.search(criterion) is not None
    has_browser_runtime = _BROWSER_RUNTIME.search(criterion) is not None
    subjective = _SUBJECTIVE.search(criterion) is not None

    if has_path:
        mechanisms.add(AcceptanceMechanism.ARTIFACT)
    if has_command and (has_command_outcome or has_verification_action):
        mechanisms.add(AcceptanceMechanism.COMMAND)
    if has_behavior:
        mechanisms.add(AcceptanceMechanism.BEHAVIOR)

    concrete_browser_target = has_browser_runtime or (has_viewport and has_viewport_size)
    browser_required = concrete_browser_target and (
        has_verification_action
        or has_evidence
        or has_behavior
        or has_command_outcome
        or _NEGATED_REQUIREMENT.search(criterion) is not None
    )
    if concrete_browser_target and (browser_required or has_behavior):
        mechanisms.add(AcceptanceMechanism.BROWSER)

    evidence_target = has_path or has_command or concrete_browser_target
    if has_evidence and has_verification_action and evidence_target:
        mechanisms.add(AcceptanceMechanism.VERIFICATION_EVIDENCE)

    # Subjective adjectives do not become observable merely by adding "evidence". A
    # concrete artifact, command, behavior, or browser target must still be present.
    if subjective and mechanisms == {AcceptanceMechanism.VERIFICATION_EVIDENCE}:
        mechanisms.clear()

    return AcceptanceCriterionAnalysis(
        mechanisms=frozenset(mechanisms),
        browser_required=browser_required,
        requires_judgment=subjective,
    )
