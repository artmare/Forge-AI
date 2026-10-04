"""Forge-owned completion evidence derived from durable runtime state."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.completion_contracts import (
    MAX_MANIFEST_ENTRIES,
    MAX_MANIFEST_SUMMARY,
    AcceptanceEvidence,
    ArtifactEvidence,
    ArtifactProvenance,
    CompletionManifest,
    CompletionReadiness,
    ManifestEvidenceStatus,
    VerificationEvidence,
    criterion_requires_judgment,
)
from app.development.completion_safety import (
    criterion_paths,
    criterion_requires_static_verification,
    required_deliverables,
)
from app.development.static_web_verifier import StaticWebVerifier
from app.development.verification_contracts import (
    VerificationResult,
    VerificationStepStatus,
    VisualQADecision,
    VisualQAEvidence,
)
from app.development.verification_plan import VerificationPlanService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    QADecision,
    ToolCallStatus,
)
from app.domain.models import (
    AcceptanceVerification,
    DevelopmentExecution,
    Event,
    QAResult,
    Task,
    TaskRun,
    ToolCall,
)
from app.tool_system.contracts import canonical_git_reference
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager

_MAX_ENTRIES = MAX_MANIFEST_ENTRIES
_MAX_SUMMARY = MAX_MANIFEST_SUMMARY
_VERIFY_ACTIONS = frozenset(
    {
        DevelopmentAction.NODE_TEST,
        DevelopmentAction.NODE_BUILD,
        DevelopmentAction.NODE_LINT,
        DevelopmentAction.NODE_TYPECHECK,
        DevelopmentAction.PYTHON_TEST,
        DevelopmentAction.PYTHON_LINT,
    }
)


class CompletionManifestService:
    """Derive completion readiness from Forge-owned data and current workspace state."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.workspaces = WorkspaceManager(self.settings.tool_workspace_root)

    async def build(self, task_id: UUID, task_run_id: UUID) -> CompletionManifest:
        task = await self.session.get(Task, task_id)
        task_run = await self.session.get(TaskRun, task_run_id)
        if (
            task is None
            or task_run is None
            or task_run.task_id != task.id
            or task.project_id is None
        ):
            raise RuntimeError("Completion manifest requires a task-scoped project TaskRun")
        workspace = self.workspaces.existing_project_workspace(task.company_id, task.project_id)
        if workspace is None:
            raise RuntimeError("Completion manifest requires an existing authorized workspace")

        recovery, checkpoint = await self._recovery_provenance(task.id)
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_id == task.id,
                    ToolCall.status == ToolCallStatus.SUCCEEDED,
                    ToolCall.tool_name.in_(("filesystem.write", "filesystem.patch")),
                )
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        required = self._required_deliverables(task)
        artifacts = await self._artifacts(
            task, task_run, calls, required, recovery, checkpoint
        )
        verifications = await self._verifications(task, task_run)
        static_results = await StaticWebVerifier().verify(workspace, artifacts)
        verifications.extend(static_results)
        verifications = verifications[:_MAX_ENTRIES]
        plan = await VerificationPlanService(self.session, self.settings).latest(task.id)
        automatic = await self._automatic_results(task.id, plan.source_generation if plan else None)
        visual = await self._visual_evidence(task.id, plan.source_generation if plan else None)
        acceptance = await self._acceptance(task, task_run, artifacts, verifications)
        if plan is not None:
            automatic_by_key = {item.step_key: item for item in automatic}
            acceptance_by_index = {item.criterion_index: item for item in acceptance}
            for step in plan.steps:
                result = automatic_by_key.get(step.key)
                if (
                    not step.deterministic
                    or result is None
                    or result.status != VerificationStepStatus.PASSED
                ):
                    continue
                for criterion_index in step.criterion_indices:
                    item = acceptance_by_index.get(criterion_index)
                    if item is None or item.status != ManifestEvidenceStatus.UNVERIFIED:
                        continue
                    item.status = ManifestEvidenceStatus.VERIFIED
                    item.requires_judgment = False
                    item.evidence_summary = (
                        f"Verified by Forge plan step {step.kind.value}: {result.summary}"
                    )[:_MAX_SUMMARY]
                    evidence_id = result.tool_call_id or result.execution_id
                    item.evidence_ids = [str(evidence_id)] if evidence_id else []
        if visual is not None and visual.decision == VisualQADecision.ACCEPT:
            for item in acceptance:
                if (
                    item.criterion_index in visual.criteria_addressed
                    and item.requires_judgment
                    and item.status == ManifestEvidenceStatus.UNVERIFIED
                ):
                    item.status = ManifestEvidenceStatus.SUPPORTED
                    item.evidence_summary = "Supported by capture-gated structured Visual QA."
        unresolved = self._unresolved_failures(artifacts, verifications)
        unresolved.extend(await self._unresolved_tool_failures(task, task_run))
        unresolved = list(dict.fromkeys(unresolved))[:50]
        qa = await self.session.scalar(
            select(QAResult)
            .where(QAResult.task_id == task.id, QAResult.task_run_id == task_run.id)
            .order_by(QAResult.created_at.desc())
            .limit(1)
        )
        blocking = self._blocking_reasons(artifacts, verifications, acceptance)
        if plan is not None:
            by_key = {item.step_key: item for item in automatic}
            for step in plan.steps:
                if not step.blocking:
                    continue
                result = by_key.get(step.key)
                if result is None:
                    blocking.append(f"Planned verification {step.kind.value} has not executed.")
                elif result.status != VerificationStepStatus.PASSED:
                    blocking.append(
                        f"Planned verification {step.kind.value} is {result.status.value.lower()}."
                    )
        blocking.extend(
            value for value in unresolved if value not in blocking
        )
        blocking = blocking[:50]
        judgment = [
            item.criterion
            for item in acceptance
            if item.status == ManifestEvidenceStatus.UNVERIFIED
            and item.requires_judgment
        ][:_MAX_ENTRIES]
        if blocking:
            readiness = CompletionReadiness.BLOCKED
        elif qa is None:
            readiness = CompletionReadiness.NOT_READY
        elif qa.decision == QADecision.FAIL:
            readiness = CompletionReadiness.BLOCKED
            blocking = [*blocking, f"QA did not pass: {qa.failure_code or qa.summary}"][:50]
        elif judgment:
            readiness = CompletionReadiness.REQUIRES_JUDGMENT
        else:
            readiness = CompletionReadiness.READY
        return CompletionManifest(
            task_id=task.id,
            task_run_id=task_run.id,
            company_id=task.company_id,
            project_id=task.project_id,
            workspace_identity=f"{task.company_id}/{task.project_id}",
            checkpoint=checkpoint,
            artifacts=artifacts[:_MAX_ENTRIES],
            verification_results=verifications,
            verification_plan_version=plan.version if plan else None,
            verification_source_generation=plan.source_generation if plan else None,
            planned_verification=plan.steps if plan else [],
            automatic_verification=automatic,
            visual_qa=visual,
            acceptance_criteria=acceptance[:_MAX_ENTRIES],
            unresolved_failures=unresolved[:50],
            blocking_reasons=blocking[:50],
            judgment_required=judgment[:50],
            readiness=readiness,
            generated_at=datetime.now(UTC),
        )

    async def _automatic_results(
        self, task_id: UUID, generation: str | None
    ) -> list[VerificationResult]:
        if generation is None:
            return []
        events = list(
            await self.session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.type == "DEV_VERIFICATION_RESULT")
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(100)
            )
        )
        output: dict[str, VerificationResult] = {}
        for event in events:
            raw = event.details.get("result") if isinstance(event.details, dict) else None
            try:
                result = VerificationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if result.source_generation == generation and result.step_key not in output:
                output[result.step_key] = result
        return list(reversed(list(output.values())))[:32]

    async def _visual_evidence(
        self, task_id: UUID, generation: str | None
    ) -> VisualQAEvidence | None:
        if generation is None:
            return None
        events = list(
            await self.session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.type == "DEV_VISUAL_QA_RECORDED")
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(10)
            )
        )
        for event in events:
            raw = event.details.get("evidence") if isinstance(event.details, dict) else None
            try:
                evidence = VisualQAEvidence.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if evidence.source_generation == generation:
                return evidence
        return None

    async def _artifacts(
        self,
        task: Task,
        task_run: TaskRun,
        calls: list[ToolCall],
        required: set[str],
        recovery: dict[UUID, dict[str, str]],
        checkpoint: str | None,
    ) -> list[ArtifactEvidence]:
        latest: dict[str, ToolCall] = {}
        for call in calls:
            raw = (call.result or {}).get("path") or call.arguments.get("path")
            if isinstance(raw, str):
                latest[raw] = call
        paths = sorted(set(latest) | required)[:_MAX_ENTRIES]
        artifacts: list[ArtifactEvidence] = []
        for path in paths:
            call = latest.get(path)
            is_recovery = bool(
                call is not None
                and call.id in recovery
                and recovery[call.id].get("path") == path
            )
            exists, current_hash, issue = await self._current_file(task, path)
            expected_hash = None
            if call is not None and call.tool_name == "filesystem.write":
                content = call.arguments.get("content")
                if isinstance(content, str):
                    expected_hash = hashlib.sha256(content.encode()).hexdigest()
            if call is None:
                provenance = ArtifactProvenance.WORKSPACE_DISCOVERY
                status = (
                    ManifestEvidenceStatus.SUPPORTED
                    if exists
                    else ManifestEvidenceStatus.MISSING
                )
                issue = issue or (None if exists else "Required deliverable is missing.")
            elif call.task_run_id == task_run.id:
                provenance = ArtifactProvenance.CURRENT_TASK_RUN
                status, issue = self._mutation_status(
                    exists, current_hash, expected_hash, call.tool_name, issue
                )
            elif is_recovery:
                assert call is not None
                provenance = ArtifactProvenance.RECOVERY_HISTORY
                recovery_item = recovery[call.id]
                recovery_hash = recovery_item.get("sha256")
                status, issue = self._mutation_status(
                    exists, current_hash, recovery_hash or expected_hash, call.tool_name, issue
                )
            else:
                provenance = ArtifactProvenance.TASK_HISTORY
                status, issue = self._mutation_status(
                    exists, current_hash, expected_hash, call.tool_name, issue
                )
            artifacts.append(
                ArtifactEvidence(
                    path=path,
                    exists=exists,
                    sha256=current_hash,
                    status=status,
                    provenance=provenance,
                    originating_tool_call_id=call.id if call else None,
                    originating_agent_run_id=call.agent_run_id if call else None,
                    originating_task_run_id=call.task_run_id if call else None,
                    checkpoint=checkpoint if is_recovery else None,
                    checkpoint_present=bool(is_recovery and checkpoint),
                    required=path in required,
                    issue=issue,
                )
            )
        return artifacts

    async def _current_file(self, task: Task, path: str) -> tuple[bool, str | None, str | None]:
        try:
            _, candidate = self.workspaces.resolve(
                task.company_id, task.project_id, path, must_exist=True  # type: ignore[arg-type]
            )
            self.workspaces.ensure_regular_file(candidate)
            digest = await asyncio.to_thread(self._sha256_file, candidate)
            return True, digest, None
        except (OSError, ToolSystemError):
            return False, None, "Artifact is missing or is not a safe regular workspace file."

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(131072), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _mutation_status(
        exists: bool,
        current_hash: str | None,
        expected_hash: str | None,
        tool_name: str,
        issue: str | None,
    ) -> tuple[ManifestEvidenceStatus, str | None]:
        if not exists:
            return ManifestEvidenceStatus.MISSING, issue or "Artifact no longer exists."
        if expected_hash is None:
            return (
                ManifestEvidenceStatus.SUPPORTED,
                "Current file exists, but the mutation has no deterministic post-write hash."
                if tool_name == "filesystem.patch"
                else "Current file exists without deterministic content provenance.",
            )
        if current_hash != expected_hash:
            return (
                ManifestEvidenceStatus.INVALIDATED,
                "Current content hash differs from the authoritative mutation evidence.",
            )
        return ManifestEvidenceStatus.VERIFIED, None

    async def _recovery_provenance(
        self, task_id: UUID
    ) -> tuple[dict[UUID, dict[str, str]], str | None]:
        event = await self.session.scalar(
            select(Event)
            .where(Event.task_id == task_id, Event.type == "DEV_RECOVERY_EVIDENCE_VERIFIED")
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        review_ready = await self.session.scalar(
            select(Event)
            .where(Event.task_id == task_id, Event.type == "DEV_WORKTREE_REVIEW_READY")
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        checkpoint_value = (
            review_ready.details.get("checkpoint_commit")
            if review_ready is not None
            else (event.details.get("checkpoint") if event is not None else None)
        )
        checkpoint = (
            str(checkpoint_value)[:80] if isinstance(checkpoint_value, str) else None
        )
        if event is None:
            return {}, checkpoint
        verified: dict[UUID, dict[str, str]] = {}
        for raw in event.details.get("verified", [])[:_MAX_ENTRIES]:
            if not isinstance(raw, dict):
                continue
            try:
                call_id = UUID(str(raw.get("original_tool_call_id")))
            except (TypeError, ValueError):
                continue
            path = raw.get("path")
            sha256 = raw.get("sha256")
            if isinstance(path, str) and isinstance(sha256, str):
                verified[call_id] = {"path": path, "sha256": sha256}
        return verified, checkpoint

    async def _verifications(
        self, task: Task, task_run: TaskRun
    ) -> list[VerificationEvidence]:
        executions = list(
            await self.session.scalars(
                select(DevelopmentExecution)
                .where(
                    DevelopmentExecution.task_id == task.id,
                    DevelopmentExecution.task_run_id == task_run.id,
                )
                .order_by(DevelopmentExecution.created_at, DevelopmentExecution.id)
            )
        )
        latest_execution: dict[DevelopmentAction, DevelopmentExecution] = {}
        for execution in executions:
            if execution.action in _VERIFY_ACTIONS or execution.action.value.startswith("GIT_"):
                latest_execution[execution.action] = execution
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(ToolCall.task_id == task.id, ToolCall.task_run_id == task_run.id)
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        calls_by_execution = {
            str(call.result.get("execution_id")): call
            for call in calls
            if isinstance(call.result, dict) and call.result.get("execution_id")
        }
        output = [
            self._execution_evidence(item, calls_by_execution.get(str(item.id)))
            for item in latest_execution.values()
        ]
        latest_tools: dict[str, ToolCall] = {}
        for call in calls:
            reference = canonical_git_reference(call.tool_name)
            if reference:
                latest_tools[f"GIT:{reference}"] = call
            elif call.tool_name == "browser.capture":
                path = (call.result or {}).get("path") or call.arguments.get("path") or "index.html"
                latest_tools[f"BROWSER:{path}"] = call
        for key, call in latest_tools.items():
            if key.startswith("GIT:") and any(
                item.reference == key.removeprefix("GIT:") for item in output
            ):
                continue
            output.append(self._tool_evidence(call))
        return output

    @staticmethod
    def _execution_evidence(
        execution: DevelopmentExecution, call: ToolCall | None = None
    ) -> VerificationEvidence:
        passed = execution.status == DevelopmentExecutionStatus.SUCCEEDED
        summary = (
            f"{execution.action.value} exited {execution.exit_code}; "
            f"output_bytes={execution.stdout_bytes + execution.stderr_bytes}; "
            f"truncated={execution.output_truncated}."
        )
        return VerificationEvidence(
            kind="GIT" if execution.action.value.startswith("GIT_") else "DEVELOPMENT",
            reference=execution.action.value,
            status=(ManifestEvidenceStatus.VERIFIED if passed else ManifestEvidenceStatus.FAILED),
            execution_id=execution.id,
            tool_call_id=call.id if call is not None else None,
            agent_run_id=execution.agent_run_id,
            task_run_id=execution.task_run_id,
            exit_code=execution.exit_code,
            summary=summary[:_MAX_SUMMARY],
            origin=execution.execution_origin,
            recorded_at=execution.finished_at or execution.created_at,
        )

    @staticmethod
    def _tool_evidence(call: ToolCall) -> VerificationEvidence:
        successful = call.status == ToolCallStatus.SUCCEEDED
        result = call.result or {}
        if call.tool_name == "browser.capture":
            rendered = result.get("rendered") is True
            errors = result.get("console_errors")
            error_count = len(errors) if isinstance(errors, list) else 0
            successful = successful and rendered and error_count == 0
            reference = str(result.get("artifact") or result.get("path") or "browser.capture")
            summary = (
                f"Browser rendered={rendered}; console_errors={error_count}; "
                f"artifact_sha256={str(result.get('sha256') or '')[:64]}."
            )
            kind = "BROWSER"
        else:
            reference = canonical_git_reference(call.tool_name) or call.tool_name
            summary = f"Forge {call.tool_name} ToolCall completed with status {call.status.value}."
            kind = "GIT"
        return VerificationEvidence(
            kind=kind,
            reference=reference[:512],
            status=(
                ManifestEvidenceStatus.VERIFIED
                if successful
                else ManifestEvidenceStatus.FAILED
            ),
            tool_call_id=call.id,
            agent_run_id=call.agent_run_id,
            task_run_id=call.task_run_id,
            exit_code=result.get("exit_code") if isinstance(result.get("exit_code"), int) else None,
            summary=summary[:_MAX_SUMMARY],
            origin="TOOL_CALL",
            recorded_at=call.completed_at,
        )

    async def _acceptance(
        self,
        task: Task,
        task_run: TaskRun,
        artifacts: list[ArtifactEvidence],
        verifications: list[VerificationEvidence],
    ) -> list[AcceptanceEvidence]:
        persisted = list(
            await self.session.scalars(
                select(AcceptanceVerification)
                .where(
                    AcceptanceVerification.task_id == task.id,
                    AcceptanceVerification.task_run_id == task_run.id,
                    AcceptanceVerification.iteration == task_run.iteration,
                )
                .order_by(AcceptanceVerification.criterion_index)
            )
        )
        if persisted:
            return [
                AcceptanceEvidence(
                    criterion_index=item.criterion_index,
                    criterion=item.criterion[:1000],
                    status=self._acceptance_status(item.status),
                    evidence_summary=item.evidence_summary[:_MAX_SUMMARY],
                    evidence_ids=[str(value)[:100] for value in item.execution_ids[:20]],
                    requires_judgment=(
                        item.status == AcceptanceVerificationStatus.UNVERIFIED
                        and criterion_requires_judgment(item.criterion)
                    ),
                )
                for item in persisted[:_MAX_ENTRIES]
            ]
        by_reference = {item.reference: item for item in verifications}
        static_by_path = {
            item.reference: item for item in verifications if item.kind == "STATIC_WEB"
        }
        by_path = {item.path: item for item in artifacts}
        return [
            self._derive_acceptance(
                index, str(raw), by_path, by_reference, static_by_path
            )
            for index, raw in enumerate(task.acceptance_criteria[:_MAX_ENTRIES])
        ]

    @staticmethod
    def _acceptance_status(status: AcceptanceVerificationStatus) -> ManifestEvidenceStatus:
        return {
            AcceptanceVerificationStatus.PASSED: ManifestEvidenceStatus.VERIFIED,
            AcceptanceVerificationStatus.FAILED: ManifestEvidenceStatus.FAILED,
            AcceptanceVerificationStatus.NOT_APPLICABLE: ManifestEvidenceStatus.NOT_APPLICABLE,
            AcceptanceVerificationStatus.UNVERIFIED: ManifestEvidenceStatus.UNVERIFIED,
        }[status]

    @classmethod
    def _derive_acceptance(
        cls,
        index: int,
        criterion: str,
        by_path: dict[str, ArtifactEvidence],
        by_reference: dict[str, VerificationEvidence],
        static_by_path: dict[str, VerificationEvidence],
    ) -> AcceptanceEvidence:
        lower = criterion.lower()
        action = None
        if "typecheck" in lower or "type check" in lower:
            action = DevelopmentAction.NODE_TYPECHECK.value
        elif "lint" in lower:
            action = next(
                (name for name in ("NODE_LINT", "PYTHON_LINT") if name in by_reference), None
            )
        elif "build" in lower:
            action = DevelopmentAction.NODE_BUILD.value
        elif "test" in lower:
            action = next(
                (name for name in ("NODE_TEST", "PYTHON_TEST") if name in by_reference), None
            )
        if action and action in by_reference:
            evidence = by_reference[action]
            return AcceptanceEvidence(
                criterion_index=index,
                criterion=criterion[:1000],
                status=evidence.status,
                evidence_summary=evidence.summary,
                evidence_ids=[str(evidence.execution_id)] if evidence.execution_id else [],
            )
        paths = criterion_paths(criterion)
        if paths:
            artifacts = [by_path.get(path) for path in paths]
            failed_paths = [
                path
                for path, artifact in zip(paths, artifacts, strict=True)
                if artifact is None
                or artifact.status
                in {
                    ManifestEvidenceStatus.FAILED,
                    ManifestEvidenceStatus.INVALIDATED,
                    ManifestEvidenceStatus.MISSING,
                    ManifestEvidenceStatus.UNVERIFIED,
                }
            ]
            static_required = criterion_requires_static_verification(criterion)
            html_paths = [path for path in paths if Path(path).suffix.lower() in {".html", ".htm"}]
            static_evidence = [static_by_path.get(path) for path in html_paths]
            static_failed = [
                item
                for item in static_evidence
                if item is not None and item.status == ManifestEvidenceStatus.FAILED
            ]
            static_missing = static_required and (
                not html_paths or any(item is None for item in static_evidence)
            )
            evidence_ids = [
                str(artifact.originating_tool_call_id)
                for artifact in artifacts
                if artifact is not None and artifact.originating_tool_call_id is not None
            ][:20]
            if failed_paths or static_failed:
                reasons = []
                if failed_paths:
                    reasons.append("missing or invalid artifacts: " + ", ".join(failed_paths))
                if static_failed:
                    reasons.append(
                        "static reference verification failed: "
                        + ", ".join(item.reference for item in static_failed)
                    )
                return AcceptanceEvidence(
                    criterion_index=index,
                    criterion=criterion[:1000],
                    status=ManifestEvidenceStatus.FAILED,
                    evidence_summary=("; ".join(reasons))[:_MAX_SUMMARY],
                    evidence_ids=evidence_ids,
                )
            if static_missing:
                return AcceptanceEvidence(
                    criterion_index=index,
                    criterion=criterion[:1000],
                    status=ManifestEvidenceStatus.UNVERIFIED,
                    evidence_summary=(
                        "Artifact existence is supported, but required static link verification "
                        "has no authoritative result."
                    ),
                    evidence_ids=evidence_ids,
                )
            if criterion_requires_judgment(criterion):
                return AcceptanceEvidence(
                    criterion_index=index,
                    criterion=criterion[:1000],
                    status=ManifestEvidenceStatus.UNVERIFIED,
                    evidence_summary=(
                        "Deterministic artifact requirements are supported; semantic or visual "
                        "requirements still require judgment."
                    ),
                    evidence_ids=evidence_ids,
                    requires_judgment=True,
                )
            fully_verified = all(
                artifact is not None and artifact.status == ManifestEvidenceStatus.VERIFIED
                for artifact in artifacts
            ) and all(
                item is not None and item.status == ManifestEvidenceStatus.VERIFIED
                for item in static_evidence
            )
            return AcceptanceEvidence(
                criterion_index=index,
                criterion=criterion[:1000],
                status=(
                    ManifestEvidenceStatus.VERIFIED
                    if fully_verified
                    else ManifestEvidenceStatus.SUPPORTED
                ),
                evidence_summary=(
                    "All required artifact paths are current and"
                    + (" static references passed." if static_required else " supported.")
                )[:_MAX_SUMMARY],
                evidence_ids=evidence_ids,
            )
        return AcceptanceEvidence(
            criterion_index=index,
            criterion=criterion[:1000],
            status=ManifestEvidenceStatus.UNVERIFIED,
            evidence_summary=(
                "No deterministic Forge verifier maps to this semantic or visual criterion."
            ),
            requires_judgment=criterion_requires_judgment(criterion),
        )

    @staticmethod
    def _required_deliverables(task: Task) -> set[str]:
        return set(
            required_deliverables(
                task.input if isinstance(task.input, dict) else {}, task.acceptance_criteria
            )
        )

    @staticmethod
    def _unresolved_failures(
        artifacts: list[ArtifactEvidence], verifications: list[VerificationEvidence]
    ) -> list[str]:
        values = [
            f"{item.path}: {item.issue or item.status.value}"
            for item in artifacts
            if item.status
            in {ManifestEvidenceStatus.INVALIDATED, ManifestEvidenceStatus.MISSING}
        ]
        values.extend(
            f"{item.reference}: {item.summary}"
            for item in verifications
            if item.status == ManifestEvidenceStatus.FAILED
        )
        return [value[:_MAX_SUMMARY] for value in values]

    async def _unresolved_tool_failures(
        self, task: Task, task_run: TaskRun
    ) -> list[str]:
        relevant = {
            "filesystem.write",
            "filesystem.patch",
            "development.execute",
            "browser.capture",
            "git.commit",
        }
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_id == task.id,
                    ToolCall.task_run_id == task_run.id,
                    ToolCall.tool_name.in_(relevant),
                )
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        unresolved: dict[str, str] = {}
        for call in calls:
            discriminator = (
                call.arguments.get("path")
                or call.arguments.get("action")
                or call.tool_name
            )
            key = f"{call.tool_name}:{discriminator}"
            if call.status == ToolCallStatus.SUCCEEDED:
                unresolved.pop(key, None)
                continue
            if call.status not in {
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }:
                continue
            error = call.error if isinstance(call.error, dict) else {}
            code = str(error.get("code") or call.status.value)[:100]
            message = str(error.get("message") or "Tool execution did not succeed")[:300]
            unresolved[key] = f"{call.tool_name} ({code}): {message}"[:_MAX_SUMMARY]
        return list(unresolved.values())[:50]

    @staticmethod
    def _blocking_reasons(
        artifacts: list[ArtifactEvidence],
        verifications: list[VerificationEvidence],
        acceptance: list[AcceptanceEvidence],
    ) -> list[str]:
        reasons = [
            f"Artifact {item.path} is {item.status.value.lower()}."
            for item in artifacts
            if item.status
            in {
                ManifestEvidenceStatus.INVALIDATED,
                ManifestEvidenceStatus.MISSING,
                ManifestEvidenceStatus.UNVERIFIED,
            }
            and (item.required or item.originating_tool_call_id is not None)
        ]
        reasons.extend(
            f"Verification {item.reference} failed."
            for item in verifications
            if item.status == ManifestEvidenceStatus.FAILED
        )
        reasons.extend(
            f"Acceptance criterion {item.criterion_index + 1} failed."
            for item in acceptance
            if item.status == ManifestEvidenceStatus.FAILED
        )
        reasons.extend(
            (
                f"Acceptance criterion {item.criterion_index + 1} lacks required "
                "deterministic evidence."
            )
            for item in acceptance
            if item.status == ManifestEvidenceStatus.UNVERIFIED
            and not item.requires_judgment
        )
        return reasons[:50]

    @staticmethod
    def serialized_size(manifest: CompletionManifest, *, for_model: bool = False) -> int:
        payload = manifest.model_summary() if for_model else manifest.bounded_summary()
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
