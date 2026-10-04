from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.automatic_verification import AutomaticVerificationRunner
from app.development.bootstrap import DevelopmentPreconditions, ProjectBootstrapService
from app.development.completion_contracts import criterion_requires_judgment
from app.development.completion_safety import (
    criterion_paths,
    criterion_requires_static_verification,
)
from app.development.product_qa import ProductQAService
from app.development.profile import DevelopmentProfileService
from app.development.registry import CommandRegistry
from app.development.runner_client import RunnerClient
from app.development.verification_contracts import (
    VerificationKind,
    VerificationResult,
    VerificationStepStatus,
)
from app.development.verification_plan import VerificationPlanService
from app.domain.enums import (
    AcceptanceVerificationStatus,
    AgentRunStatus,
    AgentStatus,
    DevelopmentAction,
    DevelopmentExecutionStatus,
    DevelopmentProjectType,
    QADecision,
    TaskKind,
    TaskStatus,
    ToolCallStatus,
)
from app.domain.exceptions import DevelopmentInfrastructureError
from app.domain.models import (
    AcceptanceVerification,
    Agent,
    AgentRun,
    QAResult,
    Task,
    TaskRun,
    ToolCall,
)
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine
from app.tool_system.contracts import ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager

_WRITE_RUNTIME_INFRASTRUCTURE_CODES = frozenset(
    {
        "FILE_NOT_FOUND",
        "TOOL_ALREADY_RUNNING",
        "TOOL_DISABLED",
        "TOOL_EXECUTION_FAILED",
        "TOOL_NOT_FOUND",
        "TOOL_OUTPUT_VALIDATION_FAILED",
        "TOOL_STATE_CONFLICT",
        "TOOL_TIMEOUT",
        "WORKSPACE_NOT_AVAILABLE",
    }
)


class DevelopmentQAService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        runner: RunnerClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.runner = runner
        self.registry = CommandRegistry(self.settings)
        self.profiles = DevelopmentProfileService(session, self.settings)
        self.events = EventFactory(session)
        self.workspace = WorkspaceManager(self.settings.tool_workspace_root)

    async def evaluate(self, task_id: UUID, task_run_id: UUID) -> QAResult:
        task = await self.session.get(Task, task_id)
        task_run = await self.session.get(TaskRun, task_run_id)
        if (
            task is None
            or task_run is None
            or task.kind != TaskKind.DEVELOPMENT
            or task.status != TaskStatus.IN_PROGRESS
            or task.project_id is None
        ):
            raise RuntimeError("Development QA requires an active development TaskRun")
        qa_agent = await self.session.scalar(
            select(Agent)
            .where(
                Agent.company_id == task.company_id,
                Agent.role == "QA",
                Agent.status.not_in((AgentStatus.STOPPED, AgentStatus.FAILED)),
            )
            .order_by(Agent.created_at, Agent.id)
            .limit(1)
        )
        if qa_agent is None:
            return await self._record_unavailable(
                task,
                task_run,
                "No QA Agent is configured.",
                code="QA_AGENT_UNAVAILABLE",
            )
        required_permissions = {"development.execute", "git.read"}
        if any(
            qa_agent.permissions.get(permission) is not True for permission in required_permissions
        ):
            return await self._record_unavailable(
                task,
                task_run,
                "QA Agent lacks development.execute or git.read permission.",
                code="QA_PERMISSION_UNAVAILABLE",
            )
        try:
            preconditions = await ProjectBootstrapService(
                self.session, settings=self.settings, runner=self.runner
            ).prepare_for_qa(task.id)
        except DevelopmentInfrastructureError as exc:
            return await self._record_unavailable(
                task,
                task_run,
                exc.message,
                code=exc.code,
                verifier_agent_id=qa_agent.id,
            )
        if preconditions.infrastructure_errors:
            issue = preconditions.infrastructure_errors[0]
            return await self._record_unavailable(
                task,
                task_run,
                issue["message"],
                code=issue["code"],
                verifier_agent_id=qa_agent.id,
            )
        write_runtime_failure = await self._unresolved_write_runtime_failure(task_run.id)
        if write_runtime_failure is not None:
            return await self._record_unavailable(
                task,
                task_run,
                (
                    "filesystem.write could not durably complete through the Forge tool runtime: "
                    f"{write_runtime_failure['message']}"
                ),
                code=write_runtime_failure["code"],
                verifier_agent_id=qa_agent.id,
            )
        qa_run = AgentRun(
            task_run_id=task_run.id,
            task_id=task.id,
            agent_id=qa_agent.id,
            status=AgentRunStatus.RUNNING,
            provider="deterministic",
            model_alias="qa-gate",
            model_id="forge-development-qa-v1",
            request={
                "stored": False,
                "metadata": {"is_qa": "true", "iteration": str(task.iteration)},
            },
            started_at=datetime.now(UTC),
        )
        self.session.add(qa_run)
        await self.session.flush()
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=qa_agent.id,
            task_id=task.id,
            event_type="QA_STARTED",
            message="Independent deterministic QA started.",
            payload={"agent_run_id": str(qa_run.id), "iteration": task.iteration},
        )
        await self.session.commit()

        profile = preconditions.profile
        profile_actions = [
            action
            for action in (
                profile.lint_action,
                profile.typecheck_action,
                profile.test_action,
                profile.build_action,
            )
            if action is not None
        ]
        context = ToolExecutionContext(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            task_run_id=task_run.id,
            agent_id=qa_agent.id,
            agent_run_id=qa_run.id,
            execution_origin="FORGE_QA",
        )
        checks: list[dict[str, Any]] = []
        unavailable_actions = self._unavailable_actions(preconditions)
        for issue in preconditions.implementation_errors:
            action_name = next(
                (
                    action.value
                    for action in (
                        DevelopmentAction.NODE_TEST,
                        DevelopmentAction.NODE_BUILD,
                        DevelopmentAction.NODE_LINT,
                        DevelopmentAction.NODE_TYPECHECK,
                    )
                    if issue["message"].startswith(action.value)
                ),
                issue["code"],
            )
            checks.append(
                {
                    "name": action_name,
                    "status": "NOT_VERIFIED",
                    "evidence": issue["message"],
                    "error_code": issue["code"],
                }
            )
        if (
            not profile_actions
            and not preconditions.implementation_errors
            and profile.project_type != DevelopmentProjectType.STATIC_WEB
        ):
            checks.append(self._missing_implementation_evidence_check(profile.project_type.value))
        plan = await VerificationPlanService(self.session, self.settings).build(
            task.id, task_run.id
        )
        verification_run = await AutomaticVerificationRunner(
            self.session, settings=self.settings, runner=self.runner
        ).run(plan, context)
        executions = list(verification_run.executions)
        for verification_result in verification_run.results:
            passed = verification_result.status == VerificationStepStatus.PASSED
            checks.append(
                {
                    "name": verification_result.kind.value,
                    "status": (
                        "PASSED"
                        if passed
                        else "SKIPPED"
                        if verification_result.status == VerificationStepStatus.SKIPPED
                        else "FAILED"
                    ),
                    "evidence": verification_result.summary,
                    "execution_id": (
                        str(verification_result.execution_id)
                        if verification_result.execution_id
                        else None
                    ),
                    "tool_call_id": (
                        str(verification_result.tool_call_id)
                        if verification_result.tool_call_id
                        else None
                    ),
                    "reused": verification_result.reused,
                    "source_generation": verification_result.source_generation,
                }
            )

        await self.session.execute(
            delete(AcceptanceVerification).where(
                AcceptanceVerification.task_id == task.id,
                AcceptanceVerification.iteration == task.iteration,
            )
        )
        verifications = self._verify_criteria(
            task,
            task_run,
            qa_agent,
            executions,
            unavailable_actions,
            list(verification_run.results),
        )
        self.session.add_all(verifications)
        product_qa = await ProductQAService(self.session, self.settings).evaluate(
            task,
            task_run,
            functional_passed=bool(profile_actions)
            and all(
                execution.status == DevelopmentExecutionStatus.SUCCEEDED
                for execution in executions
                if execution.action in profile_actions
            ),
        )
        if (
            not profile_actions
            and not preconditions.implementation_errors
            and profile.project_type == DevelopmentProjectType.STATIC_WEB
            and product_qa is None
        ):
            checks.append(self._missing_implementation_evidence_check(profile.project_type.value))
        if product_qa is not None:
            self._apply_product_criteria(verifications, product_qa.dimensions)
            checks.append(
                {
                    "name": "PRODUCT_QA",
                    "status": "PASSED" if product_qa.decision == "PASS" else "FAILED",
                    "evidence": (
                        "Deterministic product checks passed; render-dependent dimensions may "
                        "remain unverified."
                        if product_qa.decision == "PASS"
                        else f"Product QA found {len(product_qa.issues)} issue(s)."
                    ),
                    "product_qa_result_id": str(product_qa.id),
                }
            )
        blocking: list[dict[str, Any]] = []
        non_blocking: list[dict[str, Any]] = []
        for check in checks:
            if check["status"] != "PASSED":
                blocking.append(
                    {
                        "title": f"{check['name']} failed",
                        "description": check["evidence"],
                        "relatedFiles": [],
                    }
                )
        for verification in verifications:
            if verification.status == AcceptanceVerificationStatus.FAILED:
                blocking.append(
                    {
                        "title": (
                            f"Acceptance criterion {verification.criterion_index + 1} "
                            "is not verified"
                        ),
                        "description": verification.evidence_summary,
                        "relatedFiles": [],
                    }
                )
            elif (
                verification.status == AcceptanceVerificationStatus.UNVERIFIED
                and criterion_requires_judgment(verification.criterion)
            ):
                non_blocking.append(
                    {
                        "title": (
                            f"Acceptance criterion {verification.criterion_index + 1} "
                            "requires judgment"
                        ),
                        "description": verification.evidence_summary,
                        "relatedFiles": [],
                    }
                )
            elif verification.status == AcceptanceVerificationStatus.UNVERIFIED:
                blocking.append(
                    {
                        "title": (
                            f"Acceptance criterion {verification.criterion_index + 1} "
                            "lacks deterministic evidence"
                        ),
                        "description": verification.evidence_summary,
                        "relatedFiles": [],
                    }
                )
        if product_qa is not None:
            for issue in product_qa.issues:
                if issue.get("severity") in {"BLOCKING", "MAJOR"}:
                    blocking.append(
                        {
                            "title": issue.get("category", "Product QA issue"),
                            "description": issue.get("description", "Product QA failed."),
                            "relatedFiles": [issue["file"]] if issue.get("file") else [],
                            "dimension": issue.get("dimension"),
                            "severity": issue.get("severity"),
                            "suggested_fix": issue.get("suggested_fix"),
                            "product_qa_result_id": str(product_qa.id),
                        }
                    )
        decision = QADecision.PASS if checks and not blocking else QADecision.FAIL
        summary = (
            (
                "All deterministic QA checks passed; "
                f"{len(non_blocking)} criterion/criteria require human judgment."
                if non_blocking
                else "All deterministic QA checks and acceptance criteria passed."
            )
            if decision == QADecision.PASS
            else f"QA found {len(blocking)} blocking issue(s)."
        )
        result = QAResult(
            task_id=task.id,
            task_run_id=task_run.id,
            verifier_agent_id=qa_agent.id,
            iteration=task.iteration,
            decision=decision,
            summary=summary,
            failure_classification=(
                None if decision == QADecision.PASS else "IMPLEMENTATION_FAILURE"
            ),
            failure_code=(
                None
                if decision == QADecision.PASS
                else (
                    preconditions.implementation_errors[0]["code"]
                    if preconditions.implementation_errors
                    else "DETERMINISTIC_QA_FAILED"
                )
            ),
            checks=checks,
            blocking_issues=blocking,
            non_blocking_issues=non_blocking,
            correlation_id=task.id,
        )
        self.session.add(result)
        await self.session.flush()
        qa_run.status = AgentRunStatus.SUCCEEDED
        qa_run.response = {
            "decision": decision.value,
            "summary": summary,
            "checks": checks,
            "blockingIssues": blocking,
            "nonBlockingIssues": non_blocking,
        }
        qa_run.completed_at = datetime.now(UTC)
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=qa_agent.id,
            task_id=task.id,
            event_type="QA_PASSED" if decision == QADecision.PASS else "QA_FAILED",
            message=summary,
            payload={
                "qa_result_id": str(result.id),
                "decision": decision.value,
                "iteration": task.iteration,
                "check_count": len(checks),
                "blocking_issue_count": len(blocking),
                "judgment_required_count": len(non_blocking),
            },
        )
        await self.session.commit()
        return result

    async def _unresolved_write_runtime_failure(self, task_run_id: UUID) -> dict[str, str] | None:
        """Return the latest Forge-owned write failure not superseded by a successful retry."""
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_run_id == task_run_id,
                    ToolCall.tool_name == "filesystem.write",
                )
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        unresolved: dict[str, dict[str, str]] = {}
        for call in calls:
            raw_path = call.arguments.get("path")
            path = raw_path if isinstance(raw_path, str) else f"tool-call:{call.id}"
            if call.status == ToolCallStatus.SUCCEEDED:
                unresolved.pop(path, None)
                continue
            error = call.error if isinstance(call.error, dict) else {}
            code = error.get("code")
            message = error.get("message")
            if (
                call.status == ToolCallStatus.FAILED
                and isinstance(code, str)
                and code in _WRITE_RUNTIME_INFRASTRUCTURE_CODES
            ):
                unresolved[path] = {
                    "code": code,
                    "message": message if isinstance(message, str) else "Tool execution failed",
                }
        return next(reversed(unresolved.values()), None) if unresolved else None

    async def _record_unavailable(
        self,
        task: Task,
        task_run: TaskRun,
        message: str,
        *,
        code: str,
        verifier_agent_id: UUID | None = None,
    ) -> QAResult:
        evidence = f"Verification blocked by Forge development runtime: {message}"
        await self.session.execute(
            delete(AcceptanceVerification).where(
                AcceptanceVerification.task_id == task.id,
                AcceptanceVerification.iteration == task.iteration,
            )
        )
        verifications = [
            AcceptanceVerification(
                task_id=task.id,
                task_run_id=task_run.id,
                criterion_index=index,
                criterion=str(criterion),
                iteration=task.iteration,
                status=AcceptanceVerificationStatus.UNVERIFIED,
                verifier="FORGE_DETERMINISTIC_QA",
                verifier_agent_id=verifier_agent_id,
                evidence_summary=evidence,
                execution_ids=[],
            )
            for index, criterion in enumerate(task.acceptance_criteria)
        ]
        self.session.add_all(verifications)
        result = QAResult(
            task_id=task.id,
            task_run_id=task_run.id,
            verifier_agent_id=verifier_agent_id,
            iteration=task.iteration,
            decision=QADecision.FAIL,
            summary=message,
            failure_classification="INFRASTRUCTURE_UNVERIFIABLE",
            failure_code=code,
            checks=[],
            blocking_issues=[
                {
                    "title": "Forge development runtime unavailable",
                    "description": evidence,
                    "relatedFiles": [],
                    "error_code": code,
                }
            ],
            non_blocking_issues=[],
            correlation_id=task.id,
        )
        self.session.add(result)
        await self.session.flush()
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            event_type="QA_FAILED",
            message=message,
            payload={
                "qa_result_id": str(result.id),
                "decision": "FAIL",
                "iteration": task.iteration,
                "failure_classification": "INFRASTRUCTURE_UNVERIFIABLE",
                "failure_code": code,
            },
        )
        await self.session.commit()
        return result

    def _verify_criteria(
        self,
        task: Task,
        task_run: TaskRun,
        qa_agent: Agent,
        executions: list[Any],
        unavailable_actions: dict[str, str],
        verification_results: list[VerificationResult],
    ) -> list[AcceptanceVerification]:
        by_action = {execution.action: execution for execution in executions}
        static_result = next(
            (item for item in verification_results if item.kind == VerificationKind.STATIC_WEB),
            None,
        )
        records: list[AcceptanceVerification] = []
        for index, raw in enumerate(task.acceptance_criteria):
            criterion = str(raw)
            lower = criterion.lower()
            status = AcceptanceVerificationStatus.UNVERIFIED
            evidence = "No deterministic evidence mapping was available for this criterion."
            related: list[str] = []
            action = None
            if "typecheck" in lower or "type check" in lower:
                action = DevelopmentAction.NODE_TYPECHECK
            elif "lint" in lower:
                action = (
                    DevelopmentAction.NODE_LINT
                    if DevelopmentAction.NODE_LINT in by_action
                    or DevelopmentAction.NODE_LINT.value in unavailable_actions
                    else DevelopmentAction.PYTHON_LINT
                )
            elif "build" in lower:
                action = DevelopmentAction.NODE_BUILD
            elif "test" in lower:
                action = (
                    DevelopmentAction.NODE_TEST
                    if DevelopmentAction.NODE_TEST in by_action
                    or DevelopmentAction.NODE_TEST.value in unavailable_actions
                    else DevelopmentAction.PYTHON_TEST
                )
            if action in by_action:
                execution = by_action[action]
                status = (
                    AcceptanceVerificationStatus.PASSED
                    if execution.status == DevelopmentExecutionStatus.SUCCEEDED
                    else AcceptanceVerificationStatus.FAILED
                )
                evidence = f"{action.value} exited {execution.exit_code}."
                related = [str(execution.id)]
            elif action is not None and action.value in unavailable_actions:
                evidence = (
                    "Verification could not run because the required deterministic action is "
                    f"unavailable: {unavailable_actions[action.value]}"
                )
            else:
                paths = criterion_paths(criterion)
                if paths:
                    missing: list[str] = []
                    for path in paths:
                        try:
                            _, candidate = self.workspace.resolve(
                                task.company_id,
                                task.project_id,  # type: ignore[arg-type]
                                path,
                                must_exist=True,
                            )
                            self.workspace.ensure_regular_file(candidate)
                        except (OSError, ToolSystemError):
                            missing.append(path)
                    static_required = criterion_requires_static_verification(criterion)
                    static_failed = static_required and (
                        static_result is None
                        or static_result.status != VerificationStepStatus.PASSED
                    )
                    requires_judgment = criterion_requires_judgment(criterion)
                    status = (
                        AcceptanceVerificationStatus.FAILED
                        if missing or static_failed
                        else AcceptanceVerificationStatus.UNVERIFIED
                        if requires_judgment
                        else AcceptanceVerificationStatus.PASSED
                    )
                    if missing:
                        evidence = "Required artifact paths are missing or unsafe: " + ", ".join(
                            missing
                        )
                    elif static_failed:
                        evidence = "Required static link verification did not pass."
                    elif requires_judgment:
                        evidence = (
                            "Deterministic artifact requirements passed; semantic or visual "
                            "requirements still require judgment."
                        )
                    else:
                        evidence = "All required artifact paths and deterministic link checks pass."
            records.append(
                AcceptanceVerification(
                    task_id=task.id,
                    task_run_id=task_run.id,
                    criterion_index=index,
                    criterion=criterion,
                    iteration=task.iteration,
                    status=status,
                    verifier="FORGE_DETERMINISTIC_QA",
                    verifier_agent_id=qa_agent.id,
                    evidence_summary=evidence,
                    execution_ids=related,
                )
            )
        return records

    @staticmethod
    def _apply_product_criteria(
        verifications: list[AcceptanceVerification], dimensions: list[dict[str, Any]]
    ) -> None:
        by_dimension = {item.get("dimension"): item for item in dimensions}
        keywords = {
            "RESPONSIVENESS": ("responsive", "mobile", "viewport", "overflow", "narrow"),
            "ACCESSIBILITY": ("accessible", "accessibility", "keyboard", "focus", "label"),
            "VISUAL": ("visual", "hierarchy", "layout", "polish"),
            "USABILITY": ("usable", "usability", "empty state", "loading state", "error state"),
        }
        for verification in verifications:
            if verification.status != AcceptanceVerificationStatus.UNVERIFIED:
                continue
            lower = verification.criterion.lower()
            dimension = next(
                (name for name, terms in keywords.items() if any(term in lower for term in terms)),
                None,
            )
            evidence = by_dimension.get(dimension) if dimension else None
            if evidence is None:
                continue
            state = evidence.get("status")
            if state == "PASS":
                verification.status = AcceptanceVerificationStatus.PASSED
            elif state == "FAIL":
                verification.status = AcceptanceVerificationStatus.FAILED
            verification.verifier = "FORGE_PRODUCT_QA"
            verification.evidence_summary = str(evidence.get("evidence", "No evidence recorded."))

    @staticmethod
    def _unavailable_actions(preconditions: DevelopmentPreconditions) -> dict[str, str]:
        actions: dict[str, str] = {}
        for issue in preconditions.implementation_errors:
            for action in (
                DevelopmentAction.NODE_TEST,
                DevelopmentAction.NODE_BUILD,
                DevelopmentAction.NODE_LINT,
                DevelopmentAction.NODE_TYPECHECK,
            ):
                if issue["message"].startswith(action.value):
                    actions[action.value] = issue["message"]
        return actions

    @staticmethod
    def _missing_implementation_evidence_check(project_type: str) -> dict[str, str]:
        return {
            "name": "DETERMINISTIC_IMPLEMENTATION_EVIDENCE",
            "status": "NOT_VERIFIED",
            "evidence": (
                f"The {project_type} profile exposes no deterministic test, build, lint, "
                "typecheck, or static product QA evidence. Git status alone cannot verify "
                "the implementation."
            ),
            "error_code": "DEVELOPMENT_VERIFICATION_UNAVAILABLE",
        }


class DevelopmentWorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        runner: RunnerClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.runner = runner

    async def finalize(self, task_id: UUID, task_run_id: UUID) -> QAResult:
        result = await DevelopmentQAService(
            self.session, settings=self.settings, runner=self.runner
        ).evaluate(task_id, task_run_id)
        from app.development.completion_contracts import CompletionReadiness
        from app.development.completion_manifest import CompletionManifestService

        manifest = await CompletionManifestService(self.session, self.settings).build(
            task_id, task_run_id
        )
        task = await self.session.get(Task, task_id)
        if task is None:
            raise RuntimeError("Development task disappeared during finalization")
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_COMPLETION_MANIFEST_EVALUATED",
            message="Forge derived completion readiness from authoritative evidence.",
            payload={
                **manifest.bounded_summary(),
                "manifest_bytes": CompletionManifestService.serialized_size(manifest),
                "model_calls_added": 0,
            },
        )
        if result.decision == QADecision.PASS and manifest.readiness in {
            CompletionReadiness.BLOCKED,
            CompletionReadiness.NOT_READY,
        }:
            result.decision = QADecision.FAIL
            result.summary = "Forge-owned completion evidence is not ready for human review."
            result.failure_classification = "IMPLEMENTATION_FAILURE"
            result.failure_code = "COMPLETION_MANIFEST_BLOCKED"
            result.blocking_issues = [
                {
                    "title": "Completion manifest blocked finalization",
                    "description": reason,
                    "relatedFiles": [],
                }
                for reason in manifest.blocking_reasons
            ] or [
                {
                    "title": "Completion manifest is not ready",
                    "description": "Required deterministic verification has not completed.",
                    "relatedFiles": [],
                }
            ]
        await self.session.commit()
        if result.decision == QADecision.PASS:
            from app.development.self_workflow import SelfDevelopmentWorkflow

            if SelfDevelopmentWorkflow.requested(task):
                await SelfDevelopmentWorkflow(self.session, self.settings).complete(task)
            await TaskStateMachine(self.session).transition(
                task_id,
                TaskStatus.REVIEW,
                (
                    "Deterministic QA passed; human judgment remains required."
                    if manifest.readiness == CompletionReadiness.REQUIRES_JUDGMENT
                    else "Forge-owned completion readiness and deterministic QA passed."
                ),
                commit=True,
            )
        else:
            if result.failure_classification == "INFRASTRUCTURE_UNVERIFIABLE":
                await TaskStateMachine(self.session).transition(
                    task_id,
                    TaskStatus.FAILED,
                    f"Forge development runtime could not verify the Task: {result.summary}",
                    commit=True,
                )
                return result
            await TaskStateMachine(self.session).transition(
                task_id,
                TaskStatus.FIX_REQUIRED,
                "Deterministic QA found blocking issues.",
                commit=False,
            )
            task = await self.session.get(Task, task_id)
            if task is not None and task.iteration < min(
                task.max_iterations, self.settings.developer_max_fix_attempts + 1
            ):
                await TaskStateMachine(self.session).transition(
                    task_id,
                    TaskStatus.QUEUED,
                    "QA findings queued for Developer revision.",
                    commit=True,
                )
            else:
                await TaskStateMachine(self.session).transition(
                    task_id,
                    TaskStatus.FAILED,
                    "Developer fix attempts exhausted after QA failure.",
                    commit=True,
                )
        return result
