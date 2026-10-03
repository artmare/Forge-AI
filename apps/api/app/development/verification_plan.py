"""Deterministic verification-plan derivation and reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.development.completion_contracts import PATH_CRITERION, criterion_requires_judgment
from app.development.profile import DevelopmentProfileService
from app.development.verification_contracts import (
    MAX_VERIFICATION_STEPS,
    VerificationKind,
    VerificationPlan,
    VerificationStep,
)
from app.domain.enums import DevelopmentAction, DevelopmentProjectType
from app.domain.models import Event, Task, TaskRun
from app.services.event_factory import EventFactory
from app.tool_system.workspace import WorkspaceManager

_ACTION_KIND = {
    DevelopmentAction.NODE_LINT: VerificationKind.NODE_LINT,
    DevelopmentAction.NODE_TYPECHECK: VerificationKind.NODE_TYPECHECK,
    DevelopmentAction.NODE_TEST: VerificationKind.NODE_TEST,
    DevelopmentAction.NODE_BUILD: VerificationKind.NODE_BUILD,
    DevelopmentAction.PYTHON_LINT: VerificationKind.PYTHON_LINT,
    DevelopmentAction.PYTHON_TEST: VerificationKind.PYTHON_TEST,
}
_ACTION_ORDER = (
    DevelopmentAction.NODE_LINT,
    DevelopmentAction.PYTHON_LINT,
    DevelopmentAction.NODE_TYPECHECK,
    DevelopmentAction.NODE_TEST,
    DevelopmentAction.PYTHON_TEST,
    DevelopmentAction.NODE_BUILD,
)
_VISUAL_TERMS = frozenset(
    {
        "visual",
        "responsive",
        "mobile",
        "desktop",
        "layout",
        "hero",
        "pricing",
        "typography",
        "professional",
        "visible",
        "spacing",
        "contrast",
        "appearance",
    }
)


class VerificationPlanService:
    """Create a bounded plan from task facts and configured project tooling."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.profiles = DevelopmentProfileService(session, self.settings)
        self.workspaces = WorkspaceManager(self.settings.tool_workspace_root)

    async def build(
        self, task_id: UUID, task_run_id: UUID, *, persist: bool = True
    ) -> VerificationPlan:
        task = await self.session.get(Task, task_id)
        task_run = await self.session.get(TaskRun, task_run_id)
        if (
            task is None
            or task_run is None
            or task_run.task_id != task.id
            or task.project_id is None
        ):
            raise RuntimeError("Verification plan requires a task-scoped project TaskRun")
        workspace = self.workspaces.project_workspace(task.company_id, task.project_id)
        profile = await self.profiles.detect(task.project_id, commit=False)
        deliverables = self._deliverables(task)
        generation = self.source_generation(workspace, deliverables)
        steps = self._steps(task, profile.project_type, profile, deliverables, workspace)
        prior = await self.latest(task.id)
        prior_keys = {step.key for step in prior.steps} if prior else set()
        new_keys = {step.key for step in steps}
        reasons: list[str] = []
        if prior is not None and prior.source_generation != generation:
            reasons.append("Workspace source generation changed.")
        if prior is not None and new_keys - prior_keys:
            reasons.append("Configured project tooling introduced required checks.")
        # Reconciliation may add checks, but never silently drops a prior blocking check.
        if prior is not None:
            by_key = {step.key: step for step in steps}
            for step in prior.steps:
                if step.blocking and step.key not in by_key:
                    by_key[step.key] = step
                    reasons.append(f"Preserved mandatory verifier {step.key}.")
            steps = sorted(by_key.values(), key=self._sort_key)[:MAX_VERIFICATION_STEPS]
        signature = self._signature(profile.project_type.value, deliverables, steps)
        prior_signature = (
            self._signature(prior.profile, prior.required_deliverables, prior.steps)
            if prior
            else None
        )
        version = (
            prior.version + 1
            if prior and signature != prior_signature
            else prior.version
            if prior
            else 1
        )
        plan = VerificationPlan(
            task_id=task.id,
            task_run_id=task_run.id,
            company_id=task.company_id,
            project_id=task.project_id,
            workspace_identity=f"{task.company_id}/{task.project_id}",
            version=min(version, 100),
            source_generation=generation,
            profile=profile.project_type.value,
            required_deliverables=deliverables,
            steps=steps,
            judgment_requirements=[
                str(value)[:500]
                for value in task.acceptance_criteria
                if criterion_requires_judgment(str(value))
            ][:50],
            reconciliation_reasons=list(dict.fromkeys(reasons))[:20],
            generated_at=datetime.now(UTC),
        )
        if persist and (
            prior is None
            or signature != prior_signature
            or prior.source_generation != generation
            or prior.task_run_id != task_run.id
        ):
            payload = plan.model_dump(mode="json")
            await EventFactory(self.session).create(
                company_id=task.company_id,
                project_id=task.project_id,
                task_id=task.id,
                correlation_id=task.id,
                event_type=(
                    "DEV_VERIFICATION_PLAN_CREATED"
                    if prior is None
                    else "DEV_VERIFICATION_PLAN_RECONCILED"
                ),
                message="Forge derived the authoritative verification plan.",
                payload={
                    "plan": payload,
                    "plan_bytes": len(json.dumps(payload, separators=(",", ":")).encode()),
                    "model_visible_bytes": len(
                        json.dumps(plan.model_projection(), separators=(",", ":")).encode()
                    ),
                    "model_calls_added": 0,
                },
            )
            await self.session.commit()
        return plan

    async def latest(self, task_id: UUID) -> VerificationPlan | None:
        event = await self.session.scalar(
            select(Event)
            .where(
                Event.task_id == task_id,
                Event.type.in_(
                    ("DEV_VERIFICATION_PLAN_CREATED", "DEV_VERIFICATION_PLAN_RECONCILED")
                ),
            )
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        if event is None or not isinstance(event.details, dict):
            return None
        raw = event.details.get("plan")
        try:
            return VerificationPlan.model_validate(raw)
        except (TypeError, ValueError):
            return None

    def _steps(
        self, task, project_type, profile, deliverables, workspace
    ) -> list[VerificationStep]:
        criteria = [str(value) for value in task.acceptance_criteria]
        all_indices = list(range(len(criteria)))
        artifact_indices = [
            index for index, criterion in enumerate(criteria) if PATH_CRITERION.search(criterion)
        ]
        steps = [
            VerificationStep(
                key="artifacts",
                kind=VerificationKind.ARTIFACTS,
                reason="Required deliverables must exist as safe regular workspace files.",
                criterion_indices=artifact_indices,
                deliverables=deliverables,
                mechanism="forge.static.artifacts",
                expected_evidence="Current path, regular-file state, and SHA-256.",
            )
        ]
        static_frontend = (
            project_type == DevelopmentProjectType.STATIC_WEB
            or any(path.endswith((".html", ".htm")) for path in deliverables)
            or any(workspace.glob("*.html"))
        )
        if static_frontend:
            steps.append(
                VerificationStep(
                    key="static-web",
                    kind=VerificationKind.STATIC_WEB,
                    reason="Local HTML references must resolve before runtime checks.",
                    criterion_indices=all_indices,
                    deliverables=deliverables,
                    mechanism="forge.static_web_verifier",
                    dependencies=["artifacts"],
                    expected_evidence="Bounded static reference report.",
                )
            )
        configured = {
            action
            for action in (
                profile.lint_action,
                profile.typecheck_action,
                profile.test_action,
                profile.build_action,
            )
            if action is not None
        }
        for action in _ACTION_ORDER:
            if action not in configured:
                continue
            kind = _ACTION_KIND[action]
            steps.append(
                VerificationStep(
                    key=kind.value.lower().replace("_", "-"),
                    kind=kind,
                    reason=f"Project configuration declares {action.value}.",
                    criterion_indices=[
                        index
                        for index, criterion in enumerate(criteria)
                        if self._criterion_matches_action(criterion, action)
                    ],
                    mechanism=f"development.execute:{action.value}",
                    dependencies=["artifacts"],
                    expected_evidence="Successful Forge DevelopmentExecution with exit status.",
                )
            )
        steps.append(
            VerificationStep(
                key="git-status",
                kind=VerificationKind.GIT_STATUS,
                reason="Human review requires a bounded authoritative repository summary.",
                criterion_indices=[],
                deterministic=True,
                blocking=True,
                mechanism="development.execute:GIT_STATUS",
                dependencies=["artifacts"],
                expected_evidence="Successful Forge Git status execution.",
            )
        )
        visual_indices = [
            index
            for index, criterion in enumerate(criteria)
            if any(term in criterion.lower() for term in _VISUAL_TERMS)
        ]
        if static_frontend and self.settings.forge_browser_enabled:
            for key, kind, width, height in (
                ("browser-desktop", VerificationKind.BROWSER_DESKTOP, 1440, 900),
                ("browser-mobile", VerificationKind.BROWSER_MOBILE, 390, 844),
            ):
                steps.append(
                    VerificationStep(
                        key=key,
                        kind=kind,
                        reason=(
                            f"Frontend verification requires a {width}x{height} rendered capture."
                        ),
                        criterion_indices=visual_indices,
                        mechanism=f"browser.capture:{width}x{height}",
                        dependencies=["static-web"],
                        expected_evidence="Rendered PNG hash, viewport, title, and console errors.",
                    )
                )
            if visual_indices:
                steps.append(
                    VerificationStep(
                        key="visual-qa",
                        kind=VerificationKind.VISUAL_QA,
                        reason="The acceptance criteria require bounded visual judgment.",
                        criterion_indices=visual_indices,
                        deterministic=False,
                        mechanism="forge.visual_qa",
                        dependencies=["browser-desktop", "browser-mobile"],
                        expected_evidence="Structured judgment tied to authoritative captures.",
                    )
                )
        return sorted(steps, key=self._sort_key)[:MAX_VERIFICATION_STEPS]

    @staticmethod
    def _criterion_matches_action(criterion: str, action: DevelopmentAction) -> bool:
        terms = {
            DevelopmentAction.NODE_LINT: ("lint",),
            DevelopmentAction.PYTHON_LINT: ("lint",),
            DevelopmentAction.NODE_TYPECHECK: ("typecheck", "type check"),
            DevelopmentAction.NODE_TEST: ("test",),
            DevelopmentAction.PYTHON_TEST: ("test",),
            DevelopmentAction.NODE_BUILD: ("build", "runnable"),
        }.get(action, ())
        return any(term in criterion.lower() for term in terms)

    @staticmethod
    def _deliverables(task: Task) -> list[str]:
        values = task.input.get("deliverables", []) if isinstance(task.input, dict) else []
        result = [value for value in values if isinstance(value, str) and 0 < len(value) <= 4096]
        for criterion in task.acceptance_criteria:
            result.extend(match.group(1) for match in PATH_CRITERION.finditer(str(criterion)))
        return sorted(dict.fromkeys(result))[:100]

    @staticmethod
    def source_generation(workspace: Path, deliverables: list[str] | None = None) -> str:
        digest = hashlib.sha256()
        selected: list[Path] = []
        names = set(deliverables or [])
        source_suffixes = {
            ".css",
            ".html",
            ".htm",
            ".js",
            ".jsx",
            ".json",
            ".jpg",
            ".jpeg",
            ".mjs",
            ".png",
            ".py",
            ".svg",
            ".ts",
            ".tsx",
            ".toml",
            ".webp",
            ".woff",
            ".woff2",
        }
        for path in workspace.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(workspace)
            if any(part in {".git", "node_modules", "dist", "build"} for part in relative.parts):
                continue
            if (
                relative.as_posix() not in names
                and path.suffix.lower() not in source_suffixes
                and path.name != "requirements.txt"
            ):
                continue
            selected.append(path)
        for path in sorted(selected)[:500]:
            relative = path.relative_to(workspace).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            size = path.stat().st_size
            if size <= 1_000_000:
                digest.update(path.read_bytes())
            else:
                with path.open("rb") as stream:
                    digest.update(str(size).encode())
                    digest.update(stream.read(65_536))
                    stream.seek(max(size - 65_536, 0))
                    digest.update(stream.read(65_536))
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _signature(profile: str, deliverables: list[str], steps: list[VerificationStep]) -> str:
        payload = {
            "profile": profile,
            "deliverables": deliverables,
            "steps": [step.model_dump(mode="json") for step in steps],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _sort_key(step: VerificationStep) -> tuple[int, str]:
        order = {
            VerificationKind.ARTIFACTS: 0,
            VerificationKind.STATIC_WEB: 1,
            VerificationKind.GIT_STATUS: 2,
            VerificationKind.NODE_LINT: 3,
            VerificationKind.PYTHON_LINT: 3,
            VerificationKind.NODE_TYPECHECK: 4,
            VerificationKind.NODE_TEST: 5,
            VerificationKind.PYTHON_TEST: 5,
            VerificationKind.NODE_BUILD: 6,
            VerificationKind.BROWSER_DESKTOP: 7,
            VerificationKind.BROWSER_MOBILE: 8,
            VerificationKind.VISUAL_QA: 9,
        }
        return order[step.kind], step.key
