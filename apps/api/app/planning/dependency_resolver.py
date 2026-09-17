import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import ExecutionJobStatus, MissionStatus, ProjectStatus, TaskStatus
from app.domain.models import ExecutionJob, Mission, Project, Task, TaskDependency
from app.repositories.task import TaskRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DependencyReconciliationResult:
    ready_tasks: int
    completed_projects: int
    terminally_blocked_tasks: int = 0
    failed_projects: int = 0


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
        terminal_upstream_ids = list(
            await self.session.scalars(
                select(Task.id)
                .where(Task.status.in_((TaskStatus.FAILED, TaskStatus.CANCELLED)))
                .order_by(Task.completed_at, Task.id)
                .limit(limit)
            )
        )
        blocked = 0
        for upstream_id in terminal_upstream_ids:
            blocked += len(
                await self.resolve_terminal_dependents(upstream_id, commit=False)
            )
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
        before_completed = set(
            await self.session.scalars(
                select(Project.id).where(Project.status == ProjectStatus.COMPLETED)
            )
        )
        before_failed = set(
            await self.session.scalars(
                select(Project.id).where(Project.status == ProjectStatus.FAILED)
            )
        )
        await self.reconcile_completions(commit=False)
        completed = len(
            set(
                await self.session.scalars(
                    select(Project.id).where(Project.status == ProjectStatus.COMPLETED)
                )
            )
            - before_completed
        )
        failed_projects = len(
            set(
                await self.session.scalars(
                    select(Project.id).where(Project.status == ProjectStatus.FAILED)
                )
            )
            - before_failed
        )
        await self.session.commit()
        if ready or completed or blocked or failed_projects:
            logger.info(
                "Dependency reconciliation completed",
                extra={
                    "event": "dependency_reconciled",
                    "ready_tasks": ready,
                    "completed_projects": completed,
                    "failed_projects": failed_projects,
                    "terminally_blocked_tasks": blocked,
                },
            )
        return DependencyReconciliationResult(
            ready,
            completed,
            blocked,
            failed_projects,
        )

    async def resolve_terminal_dependents(
        self,
        upstream_task_id: UUID,
        *,
        causation_id: UUID | None = None,
        commit: bool = True,
    ) -> list[Task]:
        """Recursively terminalize Tasks made impossible by a terminal dependency."""
        queue = [upstream_task_id]
        visited: set[UUID] = set()
        affected: list[Task] = []
        projects: set[UUID] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            dependent_ids = list(
                await self.session.scalars(
                    select(TaskDependency.task_id)
                    .where(TaskDependency.depends_on_task_id == current)
                    .order_by(TaskDependency.task_id)
                    .distinct()
                )
            )
            for task_id in dependent_ids:
                task = await self.tasks.get_for_update(task_id)
                if task is None or task.status not in {TaskStatus.CREATED, TaskStatus.QUEUED}:
                    continue
                upstream = aliased(Task)
                terminal_rows = list(
                    await self.session.execute(
                        select(upstream.id, upstream.status, upstream.terminal_reason)
                        .join(
                            TaskDependency,
                            TaskDependency.depends_on_task_id == upstream.id,
                        )
                        .where(
                            TaskDependency.task_id == task.id,
                            upstream.status.in_((TaskStatus.FAILED, TaskStatus.CANCELLED)),
                        )
                        .order_by(upstream.id)
                    )
                )
                if not terminal_rows:
                    continue
                failed_ids = [
                    row.id
                    for row in terminal_rows
                    if row.status == TaskStatus.FAILED
                    or (
                        isinstance(row.terminal_reason, dict)
                        and row.terminal_reason.get("code")
                        == "BLOCKED_BY_FAILED_DEPENDENCY"
                    )
                ]
                cancelled_ids = [
                    row.id
                    for row in terminal_rows
                    if row.status == TaskStatus.CANCELLED and row.id not in failed_ids
                ]
                failed = bool(failed_ids)
                code = (
                    "BLOCKED_BY_FAILED_DEPENDENCY"
                    if failed
                    else "BLOCKED_BY_CANCELLED_DEPENDENCY"
                )
                dependency_ids = failed_ids or cancelled_ids
                message = (
                    "Task cannot execute because required dependencies are terminal: "
                    + ", ".join(str(item) for item in dependency_ids)
                )
                task.terminal_reason = {
                    "code": code,
                    "message": message,
                    "dependency_ids": [str(item) for item in dependency_ids],
                    "propagated": True,
                }
                # A dependent that never executed is terminally cancelled, while its
                # durable reason preserves whether failure or cancellation made it
                # impossible. This keeps Phase 03 lifecycle transitions unchanged.
                target = TaskStatus.CANCELLED
                task = await TaskStateMachine(self.session).transition(
                    task.id, target, message, commit=False
                )
                pending_jobs = list(
                    await self.session.scalars(
                        select(ExecutionJob)
                        .where(
                            ExecutionJob.task_id == task.id,
                            ExecutionJob.status == ExecutionJobStatus.PENDING,
                        )
                        .with_for_update()
                    )
                )
                now = datetime.now(UTC)
                for job in pending_jobs:
                    job.status = ExecutionJobStatus.CANCELLED
                    job.completed_at = now
                    job.last_error = {"code": code, "message": message}
                await self.events.create(
                    event_type="TASK_TERMINALLY_BLOCKED",
                    message="Task became terminal because a required dependency cannot succeed.",
                    payload={
                        "task_id": str(task.id),
                        "reason": code,
                        "dependency_ids": [str(item) for item in dependency_ids],
                        "status": target.value,
                    },
                    company_id=task.company_id,
                    project_id=task.project_id,
                    agent_id=task.assigned_agent_id,
                    task_id=task.id,
                    correlation_id=task.project_id or task.id,
                    causation_id=causation_id,
                )
                affected.append(task)
                queue.append(task.id)
                if task.project_id is not None:
                    projects.add(task.project_id)
        for project_id in sorted(projects):
            await self.reconcile_project(project_id, commit=False)
        if commit:
            await self.session.commit()
        return affected

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
            ProjectStatus.FAILED,
            ProjectStatus.CANCELLED,
            ProjectStatus.ARCHIVED,
        }:
            return False
        has_tasks = await self.session.scalar(
            select(Task.id).where(Task.project_id == project.id).limit(1)
        )
        statuses = list(
            await self.session.scalars(
                select(Task.status).where(Task.project_id == project.id).order_by(Task.id)
            )
        )
        if has_tasks is None or any(
            status not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
            for status in statuses
        ):
            return False
        if TaskStatus.FAILED in statuses:
            project.status = ProjectStatus.FAILED
        elif TaskStatus.CANCELLED in statuses:
            project.status = ProjectStatus.CANCELLED
        else:
            project.status = ProjectStatus.COMPLETED
        mission = await self.session.scalar(
            select(Mission).where(Mission.project_id == project.id).with_for_update()
        )
        now = datetime.now(UTC)
        await self.events.create(
            event_type={
                ProjectStatus.COMPLETED: "PROJECT_COMPLETED",
                ProjectStatus.FAILED: "PROJECT_FAILED",
                ProjectStatus.CANCELLED: "PROJECT_CANCELLED",
            }[project.status],
            message=(
                "All Project Tasks are DONE."
                if project.status == ProjectStatus.COMPLETED
                else "Project terminated because required Tasks cannot complete."
            ),
            payload={"project_id": str(project.id), "status": project.status.value},
            company_id=project.company_id,
            project_id=project.id,
            correlation_id=mission.id if mission else project.id,
        )
        if mission is not None and mission.status in {MissionStatus.ACTIVE, MissionStatus.REVIEW}:
            mission.status = {
                ProjectStatus.COMPLETED: MissionStatus.COMPLETED,
                ProjectStatus.FAILED: MissionStatus.FAILED,
                ProjectStatus.CANCELLED: MissionStatus.CANCELLED,
            }[project.status]
            mission.completed_at = now
            if mission.status == MissionStatus.FAILED:
                mission.failed_at = now
                mission.failure_reason = "REQUIRED_TASK_FAILED"
            elif mission.status == MissionStatus.CANCELLED:
                mission.failed_at = None
                mission.failure_reason = "REQUIRED_TASK_CANCELLED"
            await self.events.create(
                event_type=(
                    "MISSION_COMPLETED"
                    if mission.status == MissionStatus.COMPLETED
                    else "MISSION_FAILED"
                    if mission.status == MissionStatus.FAILED
                    else "MISSION_CANCELLED"
                ),
                message=(
                    "Mission completed after all Project Tasks were approved."
                    if mission.status == MissionStatus.COMPLETED
                    else "Mission terminated because required Project work cannot complete."
                ),
                payload={
                    "mission_id": str(mission.id),
                    "project_id": str(project.id),
                    "status": mission.status.value,
                    "reason": mission.failure_reason,
                },
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
        task = await self.tasks.get(task_id)
        if task is not None and isinstance(task.terminal_reason, dict):
            durable = task.terminal_reason.get("code")
            if durable in {
                "BLOCKED_BY_FAILED_DEPENDENCY",
                "BLOCKED_BY_CANCELLED_DEPENDENCY",
            }:
                return str(durable)
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
