"""Automatic execution of Forge-owned deterministic verification plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.completion_contracts import (
    ArtifactEvidence,
    ArtifactProvenance,
    ManifestEvidenceStatus,
)
from app.development.service import DevelopmentExecutionService
from app.development.static_web_verifier import StaticWebVerifier
from app.development.verification_contracts import (
    VerificationKind,
    VerificationPlan,
    VerificationResult,
    VerificationStep,
    VerificationStepStatus,
    VisualQADecision,
    VisualQAEvidence,
)
from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus, ToolCallStatus
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import DevelopmentExecution, Event, Task, ToolCall
from app.services.event_factory import EventFactory
from app.tool_system.contracts import ToolExecutionContext, ToolRequestTurn
from app.tool_system.errors import ToolSystemError
from app.tool_system.service import ToolExecutionService
from app.tool_system.workspace import WorkspaceManager

_KIND_ACTION = {
    VerificationKind.NODE_LINT: DevelopmentAction.NODE_LINT,
    VerificationKind.NODE_TYPECHECK: DevelopmentAction.NODE_TYPECHECK,
    VerificationKind.NODE_TEST: DevelopmentAction.NODE_TEST,
    VerificationKind.NODE_BUILD: DevelopmentAction.NODE_BUILD,
    VerificationKind.PYTHON_LINT: DevelopmentAction.PYTHON_LINT,
    VerificationKind.PYTHON_TEST: DevelopmentAction.PYTHON_TEST,
    VerificationKind.GIT_STATUS: DevelopmentAction.GIT_STATUS,
}


@dataclass(frozen=True)
class AutomaticVerificationRun:
    results: tuple[VerificationResult, ...]
    executions: tuple[DevelopmentExecution, ...]
    tool_calls: tuple[ToolCall, ...]


class AutomaticVerificationRunner:
    """Run a plan cheap-first through Forge's existing permission/execution services."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        runner=None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.runner = runner
        self.workspaces = WorkspaceManager(self.settings.tool_workspace_root)

    async def run(
        self, plan: VerificationPlan, context: ToolExecutionContext
    ) -> AutomaticVerificationRun:
        task = await self.session.get(Task, plan.task_id)
        if task is None or task.project_id != plan.project_id:
            raise RuntimeError("Verification plan is outside the active task workspace")
        workspace = self.workspaces.project_workspace(task.company_id, task.project_id)
        prior = await self._prior_results(task.id)
        results: list[VerificationResult] = []
        executions: list[DevelopmentExecution] = []
        calls: list[ToolCall] = []
        by_key: dict[str, VerificationResult] = {}
        for step in plan.steps:
            step_fingerprint = self._step_fingerprint(step, workspace, plan)
            dependency_failure = next(
                (
                    by_key[key]
                    for key in step.dependencies
                    if key in by_key and by_key[key].status not in {VerificationStepStatus.PASSED}
                ),
                None,
            )
            if dependency_failure is not None:
                result = self._result(
                    plan,
                    step,
                    VerificationStepStatus.SKIPPED,
                    f"Skipped after blocking dependency {dependency_failure.step_key}.",
                    skipped_reason="BLOCKING_DEPENDENCY_FAILED",
                )
            elif step.key in prior and await self._reusable(
                step,
                prior[step.key],
                task.id,
                step_fingerprint,
                plan.source_generation,
            ):
                old = prior[step.key]
                result = old.model_copy(
                    update={
                        "source_generation": plan.source_generation,
                        "reused": True,
                        "recorded_at": datetime.now(UTC),
                    }
                )
                if result.execution_id is not None:
                    reused_execution = await self.session.get(
                        DevelopmentExecution, result.execution_id
                    )
                    if reused_execution is not None:
                        executions.append(reused_execution)
                if result.tool_call_id is not None:
                    reused_call = await self.session.get(ToolCall, result.tool_call_id)
                    if reused_call is not None:
                        calls.append(reused_call)
            elif step.kind == VerificationKind.ARTIFACTS:
                result = self._verify_artifacts(plan, step, workspace)
            elif step.kind == VerificationKind.STATIC_WEB:
                result = await self._verify_static(plan, step, workspace)
            elif step.kind in _KIND_ACTION:
                execution = await DevelopmentExecutionService(
                    self.session, settings=self.settings, runner=self.runner
                ).execute(_KIND_ACTION[step.kind], {}, context)
                executions.append(execution)
                passed = execution.status == DevelopmentExecutionStatus.SUCCEEDED
                result = self._result(
                    plan,
                    step,
                    VerificationStepStatus.PASSED if passed else VerificationStepStatus.FAILED,
                    (
                        f"{execution.action.value} exited {execution.exit_code} in "
                        f"{float(execution.duration_ms or 0):.0f}ms."
                    ),
                    execution_id=execution.id,
                    exit_code=execution.exit_code,
                )
            elif step.kind in {
                VerificationKind.BROWSER_DESKTOP,
                VerificationKind.BROWSER_MOBILE,
            }:
                width, height = (
                    (1440, 900) if step.kind == VerificationKind.BROWSER_DESKTOP else (390, 844)
                )
                path = self._browser_path(plan, workspace)
                call, observation = await ToolExecutionService(
                    self.session, settings=self.settings
                ).request_and_execute(
                    ToolRequestTurn(
                        type="tool_call",
                        tool_name="browser.capture",
                        arguments={"path": path, "width": width, "height": height},
                    ),
                    context,
                )
                calls.append(call)
                payload = observation.result or {}
                errors = payload.get("console_errors", [])
                passed = call.status == ToolCallStatus.SUCCEEDED and not errors
                summary = (
                    f"Rendered {width}x{height}; title={str(payload.get('title', ''))[:120]!r}; "
                    f"console_errors={len(errors) if isinstance(errors, list) else 0}."
                    if call.status == ToolCallStatus.SUCCEEDED
                    else "Browser capture did not succeed."
                )
                result = self._result(
                    plan,
                    step,
                    VerificationStepStatus.PASSED if passed else VerificationStepStatus.FAILED,
                    summary,
                    tool_call_id=call.id,
                    evidence_reference=(
                        str(payload.get("artifact"))[:512] if payload.get("artifact") else None
                    ),
                )
            else:
                from app.development.visual_qa import VisualQAService

                try:
                    visual = await VisualQAService(self.session, self.settings).review(
                        plan.task_id,
                        plan.task_run_id,
                        source_generation=plan.source_generation,
                        criterion_indices=step.criterion_indices,
                    )
                except (AgentRuntimeDomainError, ToolSystemError) as exc:
                    result = self._result(
                        plan,
                        step,
                        VerificationStepStatus.FAILED,
                        f"Visual QA unavailable: {exc.code}.",
                    )
                else:
                    accepted = visual.decision == VisualQADecision.ACCEPT
                    finding_summary = "; ".join(
                        f"{item.severity.value}:{item.category}:{item.finding}"
                        for item in visual.findings[:5]
                    )
                    result = self._result(
                        plan,
                        step,
                        VerificationStepStatus.PASSED
                        if accepted
                        else VerificationStepStatus.FAILED,
                        (
                            visual.summary
                            if not finding_summary
                            else f"{visual.summary} Findings: {finding_summary}"
                        ),
                        evidence_reference="DEV_VISUAL_QA_RECORDED",
                    )
            result = result.model_copy(update={"input_fingerprint": step_fingerprint})
            by_key[step.key] = result
            results.append(result)
            await self._record(task, result)
        await self.session.commit()
        return AutomaticVerificationRun(tuple(results), tuple(executions), tuple(calls))

    async def _prior_results(self, task_id: UUID) -> dict[str, VerificationResult]:
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
                item = VerificationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if item.step_key not in output:
                output[item.step_key] = item
        return output

    async def _record(self, task: Task, result: VerificationResult) -> None:
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_VERIFICATION_RESULT",
            message=f"Verification {result.step_key} is {result.status.value}.",
            payload={"result": result.model_dump(mode="json")},
        )

    def _verify_artifacts(
        self, plan: VerificationPlan, step: VerificationStep, workspace: Path
    ) -> VerificationResult:
        missing: list[str] = []
        for relative in plan.required_deliverables:
            try:
                candidate = (workspace / relative).resolve(strict=True)
                candidate.relative_to(workspace.resolve())
                if candidate.is_symlink() or not candidate.is_file():
                    raise OSError
            except (OSError, ValueError):
                missing.append(relative)
        return self._result(
            plan,
            step,
            VerificationStepStatus.FAILED if missing else VerificationStepStatus.PASSED,
            (
                "Missing required deliverables: " + ", ".join(missing[:20])
                if missing
                else f"Verified {len(plan.required_deliverables)} required deliverable(s)."
            ),
        )

    async def _verify_static(
        self, plan: VerificationPlan, step: VerificationStep, workspace: Path
    ) -> VerificationResult:
        paths = sorted(
            {
                *[
                    path
                    for path in plan.required_deliverables
                    if Path(path).suffix.lower() in {".html", ".htm"}
                ],
                *[path.relative_to(workspace).as_posix() for path in workspace.glob("*.html")],
            }
        )[:20]
        artifacts = [
            ArtifactEvidence(
                path=path,
                exists=True,
                sha256=self._hash(workspace / path),
                status=ManifestEvidenceStatus.SUPPORTED,
                provenance=ArtifactProvenance.WORKSPACE_DISCOVERY,
                required=path in plan.required_deliverables,
            )
            for path in paths
            if (workspace / path).is_file() and not (workspace / path).is_symlink()
        ]
        evidence = await StaticWebVerifier().verify(workspace, artifacts)
        failed = [item for item in evidence if item.status == ManifestEvidenceStatus.FAILED]
        if not evidence:
            summary = "No safe non-empty HTML entry point was available."
            status = VerificationStepStatus.FAILED
        elif failed:
            summary = "; ".join(item.summary for item in failed)[:500]
            status = VerificationStepStatus.FAILED
        else:
            summary = f"Verified {len(evidence)} static HTML entry point(s)."
            status = VerificationStepStatus.PASSED
        return self._result(plan, step, status, summary)

    async def _reusable(
        self,
        step: VerificationStep,
        result: VerificationResult,
        task_id: UUID,
        current_fingerprint: str,
        current_generation: str,
    ) -> bool:
        # Durable execution identity is rechecked so an event payload alone can never
        # manufacture successful evidence.
        if (
            result.status != VerificationStepStatus.PASSED
            or result.kind != step.kind
            or result.input_fingerprint != current_fingerprint
        ):
            return False
        if result.execution_id is not None:
            execution = await self.session.get(DevelopmentExecution, result.execution_id)
            return bool(
                execution
                and execution.task_id == task_id
                and execution.status == DevelopmentExecutionStatus.SUCCEEDED
            )
        if result.tool_call_id is not None:
            call = await self.session.get(ToolCall, result.tool_call_id)
            return bool(
                call and call.task_id == task_id and call.status == ToolCallStatus.SUCCEEDED
            )
        if step.kind == VerificationKind.VISUAL_QA:
            if result.source_generation != current_generation:
                return False
            event = await self.session.scalar(
                select(Event)
                .where(Event.task_id == task_id, Event.type == "DEV_VISUAL_QA_RECORDED")
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(1)
            )
            raw = (
                event.details.get("evidence")
                if event and isinstance(event.details, dict)
                else None
            )
            try:
                evidence = VisualQAEvidence.model_validate(raw)
            except (TypeError, ValueError):
                return False
            return bool(
                evidence.source_generation == result.source_generation
                and evidence.decision == VisualQADecision.ACCEPT
            )
        return step.kind in {VerificationKind.ARTIFACTS, VerificationKind.STATIC_WEB}

    def _step_fingerprint(
        self, step: VerificationStep, workspace: Path, plan: VerificationPlan
    ) -> str:
        suffixes: set[str] | None
        if step.kind == VerificationKind.ARTIFACTS:
            relative_paths = plan.required_deliverables
            suffixes = None
        elif step.kind in {
            VerificationKind.STATIC_WEB,
            VerificationKind.BROWSER_DESKTOP,
            VerificationKind.BROWSER_MOBILE,
            VerificationKind.VISUAL_QA,
        }:
            relative_paths = []
            suffixes = {
                ".html",
                ".htm",
                ".css",
                ".js",
                ".mjs",
                ".jsx",
                ".tsx",
                ".svg",
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".woff",
                ".woff2",
            }
        elif step.kind in {
            VerificationKind.PYTHON_LINT,
            VerificationKind.PYTHON_TEST,
        }:
            relative_paths = []
            suffixes = {".py", ".toml", ".txt"}
        elif step.kind == VerificationKind.GIT_STATUS:
            relative_paths = []
            suffixes = None
        else:
            relative_paths = []
            suffixes = {".js", ".mjs", ".jsx", ".ts", ".tsx", ".json"}
        selected: list[Path] = []
        if relative_paths:
            selected = [workspace / relative for relative in relative_paths]
        else:
            for candidate in workspace.rglob("*"):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                relative = candidate.relative_to(workspace)
                if any(
                    part in {".git", "node_modules", "dist", "build"} for part in relative.parts
                ):
                    continue
                if suffixes is not None and candidate.suffix.lower() not in suffixes:
                    continue
                selected.append(candidate)
        digest = hashlib.sha256()
        for candidate in sorted(selected)[:500]:
            try:
                relative = candidate.relative_to(workspace).as_posix()
                size = candidate.stat().st_size
                if size <= 1_000_000:
                    data = candidate.read_bytes()
                else:
                    with candidate.open("rb") as stream:
                        data = (
                            str(size).encode()
                            + stream.read(65_536)
                        )
                        stream.seek(max(size - 65_536, 0))
                        data += stream.read(65_536)
            except (OSError, ValueError):
                relative = str(candidate)
                data = b"<missing>"
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _browser_path(plan: VerificationPlan, workspace: Path) -> str:
        return next(
            (
                path
                for path in plan.required_deliverables
                if Path(path).suffix.lower() in {".html", ".htm"} and (workspace / path).is_file()
            ),
            "index.html",
        )

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _result(
        plan: VerificationPlan,
        step: VerificationStep,
        status: VerificationStepStatus,
        summary: str,
        *,
        execution_id: UUID | None = None,
        tool_call_id: UUID | None = None,
        exit_code: int | None = None,
        evidence_reference: str | None = None,
        skipped_reason: str | None = None,
    ) -> VerificationResult:
        return VerificationResult(
            step_key=step.key,
            kind=step.kind,
            status=status,
            source_generation=plan.source_generation,
            input_fingerprint=None,
            summary=summary[:500],
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            exit_code=exit_code,
            evidence_reference=evidence_reference,
            skipped_reason=skipped_reason,
            recorded_at=datetime.now(UTC),
        )
