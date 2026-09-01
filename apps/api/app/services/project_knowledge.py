from __future__ import annotations

from typing import Any

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
