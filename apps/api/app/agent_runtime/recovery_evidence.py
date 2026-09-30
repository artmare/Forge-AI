"""Revalidate task-scoped historical execution evidence for an approved recovery."""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.execution_truth import (
    EvidenceKind,
    EvidenceProvenance,
    ExecutionEvidence,
)
from app.core.config import Settings
from app.domain.enums import ToolCallStatus
from app.domain.exceptions import AgentRuntimeDomainError
from app.domain.models import AgentRun, Event, Task, ToolCall
from app.services.event_factory import EventFactory
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager

_CHECKPOINT = re.compile(r"\[[^\]]+\s+([0-9a-f]{7,40})\]")


@dataclass(frozen=True)
class RecoveryEvidenceSnapshot:
    active: bool = False
    evidence: tuple[ExecutionEvidence, ...] = ()
    approval_event_id: UUID | None = None
    handoff_event_id: UUID | None = None
    checkpoint: str | None = None
    invalidated: tuple[dict[str, str], ...] = ()
    truth_repair_attempts: int = 0

    def prompt_summary(self) -> dict[str, Any]:
        return {
            "provenance": "approved_recovery_history",
            "checkpoint": self.checkpoint,
            "verified_artifacts": [
                {
                    "path": item.reference,
                    "sha256": item.artifact_sha256,
                    "original_agent_run_id": str(item.agent_run_id),
                    "original_tool_call_id": str(item.tool_call_id),
                    "claim_scope": "HISTORICAL",
                }
                for item in self.evidence
                if item.kind == EvidenceKind.FILE_MUTATION
            ],
            "invalidated_artifacts": list(self.invalidated),
            "instruction": (
                "These writes occurred in an earlier AgentRun and remain true after a current "
                "Forge workspace/checkpoint verification. Do not claim that this AgentRun wrote "
                "them. Historical FILE_MUTATION claims must use scope HISTORICAL and the listed "
                "original_tool_call_id. QA and tests still require their own current evidence."
            ),
        }


class RecoveryEvidenceService:
    """Build evidence from real ToolCalls without copying them into a new AgentRun."""

    _APPROVAL_TYPES = (
        "DEV_CONTEXT_RESUME_APPROVED",
        "DEV_BUDGET_RESUME_APPROVED",
        "DEV_EXECUTION_TRUTH_RESUME_APPROVED",
    )

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.events = EventFactory(session)
        self.workspaces = WorkspaceManager(settings.tool_workspace_root)

    async def revalidate(self, task: Task) -> RecoveryEvidenceSnapshot:
        approval = await self.session.scalar(
            select(Event)
            .where(Event.task_id == task.id, Event.type.in_(self._APPROVAL_TYPES))
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        if approval is None:
            return RecoveryEvidenceSnapshot()
        later_runs = list(
            await self.session.scalars(
                select(AgentRun).where(
                    AgentRun.task_id == task.id,
                    AgentRun.created_at > approval.created_at,
                )
            )
        )
        if any(
            isinstance(run.response, dict) and run.response.get("status") == "completed"
            for run in later_runs
        ):
            # The approved recovery reached a final Developer result. Any later review revision
            # must establish its own current evidence instead of inheriting this approval.
            return RecoveryEvidenceSnapshot()

        handoff = await self._handoff(task.id, approval)
        if handoff is None:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_HANDOFF_MISSING",
                "Approved recovery has no task-scoped durable handoff",
                409,
            )
        checkpoint = self._checkpoint_id(handoff.details.get("checkpoint"))
        if checkpoint is None:
            # Legacy/synthetic handoffs can still resume under mutation replay protection, but
            # they receive no historical completion evidence until a checkpoint can be verified.
            return RecoveryEvidenceSnapshot(
                active=True,
                approval_event_id=approval.id,
                handoff_event_id=handoff.id,
                invalidated=(
                    {"path": "*", "reason": "CHECKPOINT_ID_UNAVAILABLE"},
                ),
            )
        if task.project_id is None:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_WORKSPACE_UNAVAILABLE",
                "Recovery task has no authorized project workspace",
                409,
            )
        try:
            workspace = self.workspaces.existing_project_workspace(
                task.company_id, task.project_id
            )
        except (OSError, ToolSystemError) as exc:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_WORKSPACE_UNSAFE",
                "Forge could not safely inspect the recovery workspace",
                409,
            ) from exc
        if workspace is None:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_WORKSPACE_UNAVAILABLE",
                "The authorized recovery workspace no longer exists",
                409,
            )
        try:
            await asyncio.to_thread(self._verify_checkpoint, workspace, checkpoint, task.id)
        except AgentRuntimeDomainError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_WORKSPACE_UNSAFE",
                "Forge could not safely inspect the recovery checkpoint",
                409,
            ) from exc

        rows = list(
            (
                await self.session.execute(
                    select(ToolCall, AgentRun)
                    .join(AgentRun, AgentRun.id == ToolCall.agent_run_id)
                    .where(
                        ToolCall.task_id == task.id,
                        AgentRun.task_id == task.id,
                        ToolCall.status == ToolCallStatus.SUCCEEDED,
                        ToolCall.tool_name.in_(("filesystem.write", "filesystem.patch")),
                        ToolCall.created_at < approval.created_at,
                    )
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            ).all()
        )
        latest: dict[str, tuple[ToolCall, AgentRun]] = {}
        invalidated: list[dict[str, str]] = []
        for call, run in rows:
            path = (call.result or {}).get("path") or call.arguments.get("path")
            if isinstance(path, str):
                latest[path] = (call, run)

        verified: list[ExecutionEvidence] = []
        verified_at = datetime.now(UTC)
        for path, (call, run) in sorted(latest.items()):
            content = call.arguments.get("content")
            if call.tool_name != "filesystem.write" or not isinstance(content, str):
                invalidated.append({"path": path, "reason": "NO_DETERMINISTIC_CONTENT_HASH"})
                continue
            expected = hashlib.sha256(content.encode()).hexdigest()
            try:
                _, current_path = self.workspaces.resolve(
                    task.company_id, task.project_id, path, must_exist=True
                )
                self.workspaces.ensure_regular_file(current_path)
                current = await asyncio.to_thread(current_path.read_bytes)
            except (OSError, ToolSystemError):
                invalidated.append({"path": path, "reason": "MISSING_OR_UNSAFE"})
                continue
            if hashlib.sha256(current).hexdigest() != expected:
                invalidated.append({"path": path, "reason": "CURRENT_CONTENT_CHANGED"})
                continue
            try:
                checkpoint_content = await asyncio.to_thread(
                    self._checkpoint_file, workspace, checkpoint, path
                )
            except (OSError, subprocess.SubprocessError):
                invalidated.append({"path": path, "reason": "CHECKPOINT_INSPECTION_FAILED"})
                continue
            if (
                checkpoint_content is None
                or hashlib.sha256(checkpoint_content).hexdigest() != expected
            ):
                invalidated.append({"path": path, "reason": "CHECKPOINT_CONTENT_MISMATCH"})
                continue
            verified.append(
                ExecutionEvidence(
                    kind=EvidenceKind.FILE_MUTATION,
                    tool=call.tool_name,
                    reference=path,
                    provenance=EvidenceProvenance.RECOVERY_HISTORY,
                    task_id=task.id,
                    project_id=task.project_id,
                    agent_run_id=run.id,
                    tool_call_id=call.id,
                    artifact_sha256=expected,
                    checkpoint=checkpoint,
                    executed_at=call.completed_at,
                    verified_at=verified_at,
                )
            )

        prior_repairs = int(
            len(
                list(
                    await self.session.scalars(
                        select(Event.id).where(
                            Event.task_id == task.id,
                            Event.type == "DEV_EXECUTION_TRUTH_REPAIR_REQUIRED",
                            Event.created_at > approval.created_at,
                        )
                    )
                )
            )
        )
        snapshot = RecoveryEvidenceSnapshot(
            active=True,
            evidence=tuple(verified),
            approval_event_id=approval.id,
            handoff_event_id=handoff.id,
            checkpoint=checkpoint,
            invalidated=tuple(invalidated),
            truth_repair_attempts=prior_repairs,
        )
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_RECOVERY_EVIDENCE_VERIFIED",
            message="Historical execution evidence was checked against the current workspace.",
            payload={
                "approval_event_id": str(approval.id),
                "handoff_event_id": str(handoff.id),
                "checkpoint": checkpoint,
                "verified": snapshot.prompt_summary()["verified_artifacts"],
                "invalidated": invalidated,
            },
        )
        await self.session.commit()
        return snapshot

    async def _handoff(self, task_id: UUID, approval: Event) -> Event | None:
        handoff_id = approval.details.get("handoff_event_id")
        if isinstance(handoff_id, str):
            try:
                parsed = UUID(handoff_id)
            except ValueError:
                return None
            return await self.session.scalar(
                select(Event).where(
                    Event.id == parsed,
                    Event.task_id == task_id,
                    Event.type.in_(("DEV_CONTEXT_FAILURE_HANDOFF", "DEV_BUDGET_HANDOFF")),
                )
            )
        return await self.session.scalar(
            select(Event)
            .where(
                Event.task_id == task_id,
                Event.type.in_(("DEV_CONTEXT_FAILURE_HANDOFF", "DEV_BUDGET_HANDOFF")),
                Event.created_at < approval.created_at,
            )
            .order_by(Event.created_at.desc())
            .limit(1)
        )

    @staticmethod
    def _checkpoint_id(value: Any) -> str | None:
        if not isinstance(value, dict) or value.get("created") is not True:
            return None
        raw = value.get("checkpoint") or value.get("commit")
        if not isinstance(raw, str):
            return None
        if re.fullmatch(r"[0-9a-f]{7,40}", raw.strip()):
            return raw.strip()
        match = _CHECKPOINT.search(raw)
        return match.group(1) if match else None

    @staticmethod
    def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            check=False,
            timeout=10,
        )

    @classmethod
    def _verify_checkpoint(cls, workspace: Path, checkpoint: str, task_id: UUID) -> None:
        commit = cls._git(workspace, "cat-file", "-e", f"{checkpoint}^{{commit}}")
        message = cls._git(workspace, "show", "-s", "--format=%B", checkpoint)
        if commit.returncode != 0 or message.returncode != 0:
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_CHECKPOINT_INVALID",
                "The recovery checkpoint is not present in the authorized repository",
                409,
            )
        if f"task:{task_id}" not in message.stdout.decode(errors="replace"):
            raise AgentRuntimeDomainError(
                "RECOVERY_EVIDENCE_CHECKPOINT_MISMATCH",
                "The recovery checkpoint does not belong to this task",
                409,
            )

    @classmethod
    def _checkpoint_file(cls, workspace: Path, checkpoint: str, path: str) -> bytes | None:
        result = cls._git(workspace, "show", f"{checkpoint}:./{path}")
        return result.stdout if result.returncode == 0 else None
