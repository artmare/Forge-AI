from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentRunStatus, TaskKind, ToolCallStatus
from app.domain.models import AgentRun, ProjectKnowledgeIndex, Task, ToolCall


class ProjectKnowledgeService:
    """Build a bounded durable project index from approved, already-persisted evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def compact_approved_task(self, task: Task) -> ProjectKnowledgeIndex | None:
        if task.project_id is None or task.kind != TaskKind.DEVELOPMENT:
            return None
        index = await self.session.scalar(
            select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == task.project_id)
        )
        if index is None:
            index = ProjectKnowledgeIndex(
                project_id=task.project_id,
                architecture_summary="Approved development knowledge is indexed by Forge.",
            )
            self.session.add(index)
            await self.session.flush()
        calls = list(
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.task_id == task.id,
                    ToolCall.tool_name == "filesystem.write",
                    ToolCall.status == ToolCallStatus.SUCCEEDED,
                )
                .order_by(ToolCall.created_at)
            )
        )
        paths = sorted(
            {
                str(call.result["path"])
                for call in calls
                if isinstance(call.result, dict) and isinstance(call.result.get("path"), str)
            }
        )
        run = await self.session.scalar(
            select(AgentRun)
            .where(AgentRun.task_id == task.id, AgentRun.status == AgentRunStatus.SUCCEEDED)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        result_summary = self._result_summary(run.response if run else None)
        change = {
            "task_id": str(task.id),
            "title": task.title,
            "iteration": task.iteration,
            "files": paths,
            "result_summary": result_summary,
        }
        modules = {str(item.get("path")): item for item in index.modules if item.get("path")}
        for path in paths:
            modules[path] = {"path": path, "last_approved_task_id": str(task.id)}
        index.modules = list(modules.values())[-200:]
        index.contracts = (
            list(index.contracts)
            + [
                {
                    "task_id": str(task.id),
                    "acceptance_criteria": [str(item) for item in task.acceptance_criteria],
                }
            ]
        )[-100:]
        index.decisions = (
            list(index.decisions)
            + [{"task_id": str(task.id), "decision": "HUMAN_APPROVED", "title": task.title}]
        )[-100:]
        index.recent_changes = (list(index.recent_changes) + [change])[-30:]
        index.state = {
            **index.state,
            "last_completed_task_id": str(task.id),
            "last_completed_task": task.title,
            "last_completed_at": datetime.now(UTC).isoformat(),
            "known_issues": index.state.get("known_issues", []),
            "next_actions": index.state.get("next_actions", []),
        }
        await self.session.flush()
        return index

    async def checkpoint(
        self,
        project_id: UUID,
        *,
        task_id: UUID,
        goal: str,
        completed: list[str],
        current_diff: str,
        decisions: list[str],
        test_status: list[str],
        failures: list[str],
        open_questions: list[str],
        next_action: str,
    ) -> ProjectKnowledgeIndex:
        """Persist a bounded L1 handoff while leaving raw execution history intact."""
        index = await self.session.scalar(
            select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == project_id)
        )
        if index is None:
            index = ProjectKnowledgeIndex(project_id=project_id, architecture_summary="")
            self.session.add(index)
            await self.session.flush()
        checkpoint = {
            "at": datetime.now(UTC).isoformat(),
            "task_id": str(task_id),
            "goal": goal[:2000],
            "completed": completed[-30:],
            "current_diff": current_diff[:20_000],
            "decisions": decisions[-20:],
            "test_status": test_status[-20:],
            "failures": failures[-20:],
            "open_questions": open_questions[-20:],
            "next_action": next_action[:2000],
        }
        index.checkpoints = [*index.checkpoints, checkpoint][-10:]
        await self.session.flush()
        return index

    async def record_verified_checkpoint(
        self, task: Task, *, checkpoint: str, changed_files: list[str]
    ) -> None:
        """Record verified state separately from later HUMAN_APPROVED knowledge."""
        if task.project_id is None:
            return
        index = await self.session.scalar(
            select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == task.project_id)
        )
        if index is None:
            index = ProjectKnowledgeIndex(project_id=task.project_id, architecture_summary="")
            self.session.add(index)
            await self.session.flush()
        change = {
            "task_id": str(task.id),
            "checkpoint": checkpoint,
            "files": changed_files[:200],
            "status": "VERIFIED_PENDING_HUMAN_REVIEW",
        }
        index.recent_changes = [*index.recent_changes, change][-30:]
        index.state = {**index.state, "pending_review": change}
        await self.session.flush()

    async def record_lesson(
        self, project_id: UUID, *, task_id: UUID, lesson: str, evidence: str
    ) -> ProjectKnowledgeIndex:
        index = await self.session.scalar(
            select(ProjectKnowledgeIndex).where(ProjectKnowledgeIndex.project_id == project_id)
        )
        if index is None:
            index = ProjectKnowledgeIndex(project_id=project_id, architecture_summary="")
            self.session.add(index)
            await self.session.flush()
        item = {
            "at": datetime.now(UTC).isoformat(),
            "task_id": str(task_id),
            "lesson": lesson[:2000],
            "evidence": evidence[:2000],
        }
        index.lessons = [*index.lessons, item][-100:]
        await self.session.flush()
        return index

    @staticmethod
    def _result_summary(response: dict[str, Any] | None) -> str:
        if not isinstance(response, dict):
            return "Approved without a model result summary."
        summary = response.get("summary")
        if not isinstance(summary, str):
            summary = response.get("output")
        return str(summary)[:1000] if summary else "Approved development result."
