"""Explicit, Forge-owned binding of a self-development task to its UUID workspace."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.development.self_development import (
    SelfDevelopmentWorkspace,
    SelfDevelopmentWorkspaceManager,
)
from app.domain.enums import ToolCallStatus
from app.domain.models import AgentRun, Event, QAResult, Task, ToolCall
from app.services.event_factory import EventFactory
from app.tool_system.errors import ToolSystemError


class SelfDevelopmentWorkflow:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session, self.settings = session, settings

    @staticmethod
    def requested(task: Task) -> bool:
        return isinstance(task.input, dict) and task.input.get("self_development") is True

    def manager(self, task: Task) -> SelfDevelopmentWorkspaceManager:
        s = self.settings
        if not self.requested(task):
            raise ToolSystemError("SELF_DEVELOPMENT_NOT_REQUESTED", "Task must explicitly opt in")
        if not (s.forge_dev_mode_enabled and s.forge_self_development_enabled):
            raise ToolSystemError("SELF_DEVELOPMENT_DISABLED", "Self-development is disabled")
        if not all(
            (
                s.forge_dev_repository_path,
                s.forge_dev_allowed_repository,
                s.forge_dev_worktree_root,
                task.project_id,
            )
        ):
            raise ToolSystemError(
                "SELF_DEVELOPMENT_CONFIGURATION", "Explicit paths and project required"
            )
        repo = Path(s.forge_dev_repository_path)
        allowed = Path(s.forge_dev_allowed_repository)
        root = Path(s.forge_dev_worktree_root)
        if (
            not allowed.is_absolute()
            or allowed.is_symlink()
            or repo != allowed
            or root != Path(s.tool_workspace_root)
        ):
            raise ToolSystemError(
                "SELF_DEVELOPMENT_CONFIGURATION",
                "Repository must equal allowlist; worktrees must use tool root",
            )
        return SelfDevelopmentWorkspaceManager(repo, root / str(task.company_id))

    async def prepare(self, task: Task) -> SelfDevelopmentWorkspace:
        manager = self.manager(task)
        event = await self.session.scalar(
            select(Event)
            .where(Event.task_id == task.id, Event.type == "DEV_WORKTREE_CREATED")
            .order_by(Event.created_at)
            .limit(1)
        )
        if event:
            workspace = SelfDevelopmentWorkspace(
                manager.worktree_root / str(task.project_id),
                f"forge-dev/{task.id}",
                event.details["base_revision"],
            )
            await manager.inspect(workspace)
            return workspace
        target = manager.worktree_root / str(task.project_id)
        # Existing context/profile reads may have created an empty UUID directory.
        if target.is_dir() and not target.is_symlink() and not any(target.iterdir()):
            target.rmdir()
        workspace = await manager.create(str(task.id), target_name=str(task.project_id))
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_WORKTREE_CREATED",
            message="Isolated self-development worktree created; human promotion required.",
            payload={
                "branch": workspace.branch,
                "worktree": str(workspace.path),
                "base_revision": workspace.base_revision,
            },
        )
        await self.session.commit()
        return workspace

    async def complete(self, task: Task) -> dict[str, str]:
        qa = await self.session.scalar(
            select(QAResult)
            .where(
                QAResult.task_id == task.id,
                QAResult.iteration == task.iteration,
            )
            .order_by(QAResult.created_at.desc())
            .limit(1)
        )
        if qa is None or qa.decision.value != "PASS":
            raise ToolSystemError("SELF_DEVELOPMENT_UNVERIFIED", "Current independent QA must pass")
        lead = await self.session.scalar(
            select(AgentRun)
            .where(
                AgentRun.task_run_id == qa.task_run_id,
                AgentRun.agent_id == task.assigned_agent_id,
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        if (
            lead is None
            or lead.status.value != "SUCCEEDED"
            or (not isinstance(lead.response, dict) or lead.response.get("status") != "completed")
        ):
            raise ToolSystemError("SELF_DEVELOPMENT_UNVERIFIED", "Lead completion is not verified")
        workspace = await self.prepare(task)
        manager = self.manager(task)
        before = await manager.inspect(workspace)
        changed = (
            await manager._git_at(
                workspace.path, "diff", "--name-only", workspace.base_revision, "--"
            )
        ).splitlines()
        untracked = (
            await manager._git_at(workspace.path, "ls-files", "--others", "--exclude-standard")
        ).splitlines()
        calls = list(
            await self.session.scalars(
                select(ToolCall).where(
                    ToolCall.task_id == task.id,
                    ToolCall.status == ToolCallStatus.SUCCEEDED,
                    ToolCall.tool_name.in_(["filesystem.write", "filesystem.patch"]),
                )
            )
        )
        evidenced = {str(c.result.get("path")) for c in calls if isinstance(c.result, dict)}
        unexpected = sorted(set(changed + untracked) - evidenced)
        if unexpected:
            raise ToolSystemError(
                "UNEXPECTED_SELF_DEVELOPMENT_FILES",
                "Changed files lack Developer mutation evidence: " + ", ".join(unexpected[:10]),
            )
        checkpoint = await manager.checkpoint(workspace)
        if (await manager.inspect(workspace))["status"].strip():
            raise ToolSystemError("UNCHECKPOINTED_WORK", "Checkpoint left uncommitted files")
        from app.services.project_knowledge import ProjectKnowledgeService

        await ProjectKnowledgeService(self.session).record_verified_checkpoint(
            task,
            checkpoint=checkpoint,
            changed_files=sorted(set(changed + untracked)),
        )
        result = {
            **before,
            "checkpoint_commit": checkpoint,
            "promotion": "HUMAN_REQUIRED",
            "changed_files": ", ".join(sorted(set(changed + untracked))),
        }
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            correlation_id=task.id,
            event_type="DEV_WORKTREE_REVIEW_READY",
            message="Verified change checkpoint prepared for human review.",
            payload=result,
        )
        await self.session.commit()
        return result

    async def git_action(self, task: Task, request):
        """Typed Git operations only; tests continue through the isolated runner."""
        from app.development.contracts import RunnerResponse
        from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus

        workspace = await self.prepare(task)
        manager = self.manager(task)
        state = await manager.inspect(workspace)
        if request.action == DevelopmentAction.GIT_STATUS:
            output = state["status"]
        elif request.action == DevelopmentAction.GIT_DIFF:
            output = await manager._git_at(
                workspace.path,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                workspace.base_revision,
                "--",
            )
        elif request.action == DevelopmentAction.GIT_LOG:
            output = await manager._git_at(workspace.path, "log", "-n", "20", "--oneline")
        elif request.action == DevelopmentAction.GIT_CHECKPOINT:
            output = await manager.checkpoint(workspace)
        else:
            raise ToolSystemError("STABLE_BRANCH_PROTECTED", "Cannot reinitialize self worktree")
        return RunnerResponse(
            request_id=request.request_id,
            status=DevelopmentExecutionStatus.SUCCEEDED,
            exit_code=0,
            stdout_excerpt=output[: request.output_limit_bytes],
            stdout_bytes=len(output.encode()),
            truncated=len(output.encode()) > request.output_limit_bytes,
        )

    async def abandon(self, task: Task) -> dict[str, str]:
        workspace = await self.prepare(task)
        state = await self.manager(task).inspect(workspace)
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            event_type="DEV_WORKTREE_ABANDONED",
            message="Worktree retained for recovery.",
            payload=state,
        )
        await self.session.commit()
        return state

    async def cleanup(self, task: Task) -> None:
        workspace = await self.prepare(task)
        await self.manager(task).cleanup(workspace)
        await EventFactory(self.session).create(
            company_id=task.company_id,
            project_id=task.project_id,
            task_id=task.id,
            event_type="DEV_WORKTREE_CLEANED",
            message="Clean worktree removed; branch retained.",
            payload={"branch": workspace.branch},
        )
        await self.session.commit()
