import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import MissionStatus, ProjectStatus, TaskStatus
from app.domain.models import Mission, Project, Task, TaskDependency
from app.repositories.task import TaskRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DependencyReconciliationResult:
    ready_tasks: int
    completed_projects: int


class DependencyResolver:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tasks = TaskRepository(session)
        self.events = EventFactory(session)

    async def resolve_dependents(
        self,
        upstream_task_id: UUID,
        *,
        causation_id: UUID | None = None,
        commit: bool = True,
    ) -> list[Task]:
        candidate_ids = list(
            await self.session.scalars(
                select(TaskDependency.task_id)
                .where(TaskDependency.depends_on_task_id == upstream_task_id)
                .order_by(TaskDependency.task_id)
                .distinct()
            )
        )
        ready: list[Task] = []
        for task_id in candidate_ids:
            task = await self._queue_if_ready(task_id, causation_id=causation_id)
            if task is not None:
                ready.append(task)
        if commit:
            await self.session.commit()
        return ready

    async def reconcile(self, limit: int = 500) -> DependencyReconciliationResult:
        task_ids = list(
            await self.session.scalars(
                select(Task.id)
                .where(
                    Task.status == TaskStatus.CREATED,
                    exists().where(TaskDependency.task_id == Task.id),
                )
                .order_by(Task.created_at, Task.id)
                .limit(limit)
            )
        )
        ready = 0
        for task_id in task_ids:
            if await self._queue_if_ready(task_id) is not None:
                ready += 1
        completed = await self.reconcile_completions(commit=False)
        await self.session.commit()
        if ready or completed:
            logger.info(
                "Dependency reconciliation completed",
                extra={
                    "event": "dependency_reconciled",
                    "ready_tasks": ready,
                    "completed_projects": completed,
                },
            )
        return DependencyReconciliationResult(ready, completed)

    async def _queue_if_ready(
        self, task_id: UUID, *, causation_id: UUID | None = None
    ) -> Task | None:
        task = await self.tasks.get_for_update(task_id)
        if task is None or task.status != TaskStatus.CREATED:
            return None
        upstream = aliased(Task)
        dependency_count = len(
            list(
                await self.session.scalars(
                    select(TaskDependency.id).where(TaskDependency.task_id == task.id)
                )
            )
        )
        if dependency_count == 0:
            return None
        unsatisfied = await self.session.scalar(
            select(TaskDependency.id)
            .join(upstream, upstream.id == TaskDependency.depends_on_task_id)
            .where(
                TaskDependency.task_id == task.id,
                upstream.status != TaskStatus.DONE,
            )
            .limit(1)
        )
        if unsatisfied is not None:
            return None
        queued = await TaskStateMachine(self.session).transition(
            task.id,
            TaskStatus.QUEUED,
            "All Task dependencies are DONE.",
            commit=False,
        )
        await self.events.create(
            event_type="TASK_BECAME_READY",
            message="All dependencies are DONE; Task became ready.",
            payload={"task_id": str(task.id), "dependency_count": dependency_count},
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            correlation_id=task.project_id or task.id,
            causation_id=causation_id,
        )
        return queued

    async def reconcile_project(self, project_id: UUID, *, commit: bool = True) -> bool:
        project = await self.session.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is None or project.status in {
            ProjectStatus.COMPLETED,
            ProjectStatus.CANCELLED,
            ProjectStatus.ARCHIVED,
        }:
            return False
        has_tasks = await self.session.scalar(
            select(Task.id).where(Task.project_id == project.id).limit(1)
        )
        unfinished = await self.session.scalar(
            select(Task.id)
            .where(Task.project_id == project.id, Task.status != TaskStatus.DONE)
            .limit(1)
        )
        if has_tasks is None or unfinished is not None:
            return False
        project.status = ProjectStatus.COMPLETED
        mission = await self.session.scalar(
            select(Mission).where(Mission.project_id == project.id).with_for_update()
        )
        now = datetime.now(UTC)
        await self.events.create(
            event_type="PROJECT_COMPLETED",
            message="All Project Tasks are DONE.",
            payload={"project_id": str(project.id)},
            company_id=project.company_id,
            project_id=project.id,
            correlation_id=mission.id if mission else project.id,
        )
        if mission is not None and mission.status in {MissionStatus.ACTIVE, MissionStatus.REVIEW}:
            mission.status = MissionStatus.COMPLETED
            mission.completed_at = now
            await self.events.create(
                event_type="MISSION_COMPLETED",
                message="Mission completed after all Project Tasks were approved.",
                payload={"mission_id": str(mission.id), "project_id": str(project.id)},
                company_id=project.company_id,
                project_id=project.id,
                correlation_id=mission.id,
            )
        if commit:
            await self.session.commit()
        return True

    async def reconcile_completions(self, *, commit: bool = True, limit: int = 200) -> int:
        project_ids = list(
            await self.session.scalars(
                select(Project.id)
                .where(Project.status.in_((ProjectStatus.ACTIVE, ProjectStatus.REVIEW)))
                .order_by(Project.created_at, Project.id)
                .limit(limit)
            )
        )
        completed = 0
        for project_id in project_ids:
            if await self.reconcile_project(project_id, commit=False):
                completed += 1
        if commit:
            await self.session.commit()
        return completed

    async def blocked_reason(self, task_id: UUID) -> str | None:
        upstream = aliased(Task)
        states = list(
            await self.session.scalars(
                select(upstream.status)
                .join(TaskDependency, TaskDependency.depends_on_task_id == upstream.id)
                .where(TaskDependency.task_id == task_id)
            )
        )
        if not states or all(state == TaskStatus.DONE for state in states):
            return None
        if TaskStatus.FAILED in states:
            return "BLOCKED_BY_FAILED_DEPENDENCY"
        if TaskStatus.CANCELLED in states:
            return "BLOCKED_BY_CANCELLED_DEPENDENCY"
        return "BLOCKED_BY_UNMET_DEPENDENCY"
