from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import (
    AgentRunStatus,
    AgentStatus,
    DevelopmentExecutionStatus,
    ExecutionJobStatus,
    MissionStatus,
    PlanningRunStatus,
    TaskRunStatus,
    TaskStatus,
    ToolCallStatus,
)
from app.domain.exceptions import EntityNotFoundError, MissionConflictError
from app.domain.models import (
    AcceptanceVerification,
    Agent,
    AgentRun,
    Company,
    DevelopmentExecution,
    Event,
    EventConsumption,
    EventDeadLetter,
    EventOutbox,
    ExecutionJob,
    Mission,
    MissionDeletionRecord,
    PlanningRun,
    Project,
    ProjectDevelopmentLease,
    QAResult,
    Task,
    TaskDependency,
    TaskReview,
    TaskRun,
    ToolCall,
)
from app.repositories.mission import MissionRepository
from app.schemas.mission import MissionDeletionResponse
from app.services.event_factory import EventFactory
from app.tool_system.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class MissionLifecycleService:
    ORGANIZABLE = frozenset(
        {
            MissionStatus.DRAFT,
            MissionStatus.PLAN_READY,
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.CANCELLED,
        }
    )
    ACTIVE_TASKS = (
        TaskStatus.QUEUED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.REVIEW,
        TaskStatus.FIX_REQUIRED,
    )
    ACTIVE_JOBS = (
        ExecutionJobStatus.PENDING,
        ExecutionJobStatus.CLAIMED,
        ExecutionJobStatus.RUNNING,
    )
    ACTIVE_AGENT_RUNS = (AgentRunStatus.CREATED, AgentRunStatus.RUNNING)
    ACTIVE_TOOL_CALLS = (
        ToolCallStatus.REQUESTED,
        ToolCallStatus.AUTHORIZED,
        ToolCallStatus.RUNNING,
    )
    ACTIVE_DEVELOPMENT = (
        DevelopmentExecutionStatus.REQUESTED,
        DevelopmentExecutionStatus.AUTHORIZED,
        DevelopmentExecutionStatus.RUNNING,
    )

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.missions = MissionRepository(session)
        self.events = EventFactory(session)

    async def archive(self, mission_id: UUID) -> Mission:
        try:
            mission = await self._locked_mission(mission_id)
            if mission.archived_at is not None:
                return mission
            self._require_organizable(mission, action="archived")
            reasons = await self._lock_and_find_active(mission)
            if reasons:
                raise MissionConflictError(
                    "MISSION_ARCHIVE_BLOCKED",
                    f"Mission cannot be archived while active state exists: {', '.join(reasons)}.",
                )
            mission.archived_at = datetime.now(UTC)
            await self.events.create(
                event_type="MISSION_ARCHIVED",
                message="Mission was archived without changing its execution state or history.",
                payload={"mission_id": str(mission.id), "status": mission.status.value},
                company_id=mission.company_id,
                project_id=mission.project_id,
                correlation_id=mission.id,
            )
            await self.session.commit()
            return mission
        except Exception:
            await self.session.rollback()
            raise

    async def restore(self, mission_id: UUID) -> Mission:
        try:
            mission = await self._locked_mission(mission_id)
            if mission.archived_at is None:
                return mission
            mission.archived_at = None
            await self.events.create(
                event_type="MISSION_RESTORED",
                message="Mission was restored to normal operational views.",
                payload={"mission_id": str(mission.id), "status": mission.status.value},
                company_id=mission.company_id,
                project_id=mission.project_id,
                correlation_id=mission.id,
            )
            await self.session.commit()
            return mission
        except Exception:
            await self.session.rollback()
            raise

    async def delete(self, mission_id: UUID, confirmation: str) -> MissionDeletionResponse:
        try:
            mission = await self._locked_mission(mission_id)
            if confirmation not in {mission.title, "DELETE"}:
                raise MissionConflictError(
                    "MISSION_DELETE_CONFIRMATION_MISMATCH",
                    "Type the Mission name or DELETE to confirm permanent deletion.",
                )
            if mission.archived_at is None:
                raise MissionConflictError(
                    "MISSION_DELETE_REQUIRES_ARCHIVE",
                    "Archive the Mission before permanently deleting it.",
                )
            self._require_organizable(mission, action="deleted")
            reasons = await self._lock_and_find_active(mission)
            if reasons:
                raise MissionConflictError(
                    "MISSION_DELETE_BLOCKED",
                    f"Mission cannot be deleted while active state exists: {', '.join(reasons)}.",
                )

            company_id = mission.company_id
            project_id = mission.project_id
            task_ids = await self._task_ids(project_id)
            agent_ids = await self._mission_agent_ids(mission)
            delete_company = await self._can_delete_company(mission, task_ids, agent_ids)
            project_deleted = project_id is not None and mission.owns_project
            record = MissionDeletionRecord(
                mission_id=mission.id,
                mission_title=mission.title,
                company_id=company_id,
                project_id=project_id,
                company_deleted=delete_company,
                project_deleted=project_deleted,
                workspace_cleanup_status=("REQUESTED" if project_deleted else "NOT_APPLICABLE"),
            )
            self.session.add(record)
            await self.events.create(
                event_type="MISSION_DELETION_REQUESTED",
                message="Permanent Mission deletion was validated and requested.",
                payload={
                    "mission_id": str(mission.id),
                    "company_will_be_deleted": delete_company,
                    "project_will_be_deleted": record.project_deleted,
                },
                correlation_id=mission.id,
            )
            await self.session.flush()
            await self._delete_topology(mission, task_ids, agent_ids, delete_company)
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        cleanup_error: str | None = None
        if company_id is not None and project_id is not None and record.project_deleted:
            try:
                WorkspaceManager(self.settings.tool_workspace_root).delete_project_workspace(
                    company_id, project_id
                )
                record.workspace_cleanup_status = "COMPLETED"
            except Exception as exc:  # DB deletion is already durable; record recovery state.
                cleanup_error = f"{type(exc).__name__}: {exc}"[:1000]
                record.workspace_cleanup_status = "CLEANUP_FAILED"
                record.workspace_cleanup_error = cleanup_error
                logger.exception(
                    "Mission database topology deleted but workspace cleanup failed",
                    extra={
                        "event": "mission_workspace_cleanup_failed",
                        "mission_id": str(mission_id),
                        "deletion_id": str(record.id),
                    },
                )
        record.completed_at = datetime.now(UTC)
        await self.events.create(
            event_type="MISSION_DELETED",
            message="Mission durable topology was permanently deleted.",
            payload={
                "mission_id": str(mission_id),
                "deletion_id": str(record.id),
                "company_deleted": record.company_deleted,
                "project_deleted": record.project_deleted,
                "workspace_cleanup_status": record.workspace_cleanup_status,
            },
            correlation_id=mission_id,
        )
        await self.session.commit()
        return MissionDeletionResponse(
            deletion_id=record.id,
            mission_id=record.mission_id,
            company_id=record.company_id,
            project_id=record.project_id,
            company_deleted=record.company_deleted,
            project_deleted=record.project_deleted,
            workspace_cleanup_status=record.workspace_cleanup_status,
            workspace_cleanup_error=cleanup_error,
        )

    async def _locked_mission(self, mission_id: UUID) -> Mission:
        mission = await self.missions.get_for_update(mission_id)
        if mission is None:
            raise EntityNotFoundError("Mission")
        return mission

    def _require_organizable(self, mission: Mission, *, action: str) -> None:
        if mission.status not in self.ORGANIZABLE:
            raise MissionConflictError(
                "MISSION_LIFECYCLE_BLOCKED",
                f"Mission in {mission.status.value} cannot be {action}; stop or cancel work first.",
            )

    async def _lock_and_find_active(self, mission: Mission) -> list[str]:
        reasons: list[str] = []
        planning = list(
            await self.session.scalars(
                select(PlanningRun)
                .where(
                    PlanningRun.mission_id == mission.id,
                    PlanningRun.status.in_((PlanningRunStatus.CREATED, PlanningRunStatus.RUNNING)),
                )
                .with_for_update()
            )
        )
        if planning:
            reasons.append("active planning")
        if mission.project_id is None:
            return reasons

        lease = await self.session.scalar(
            select(ProjectDevelopmentLease)
            .where(
                ProjectDevelopmentLease.project_id == mission.project_id,
                ProjectDevelopmentLease.lease_owner.is_not(None),
                ProjectDevelopmentLease.lease_expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
        if lease is not None:
            reasons.append("active project lease")

        tasks = list(
            await self.session.scalars(
                select(Task).where(Task.project_id == mission.project_id).with_for_update()
            )
        )
        task_ids = [task.id for task in tasks]
        if any(task.status in self.ACTIVE_TASKS for task in tasks):
            reasons.append("active tasks")
        if not task_ids:
            return reasons

        checks = (
            (
                ExecutionJob,
                ExecutionJob.task_id,
                ExecutionJob.status,
                self.ACTIVE_JOBS,
                "active jobs",
            ),
            (
                TaskRun,
                TaskRun.task_id,
                TaskRun.status,
                (TaskRunStatus.STARTED,),
                "active task runs",
            ),
            (
                AgentRun,
                AgentRun.task_id,
                AgentRun.status,
                self.ACTIVE_AGENT_RUNS,
                "active agent runs",
            ),
            (
                ToolCall,
                ToolCall.task_id,
                ToolCall.status,
                self.ACTIVE_TOOL_CALLS,
                "active tool calls",
            ),
            (
                DevelopmentExecution,
                DevelopmentExecution.task_id,
                DevelopmentExecution.status,
                self.ACTIVE_DEVELOPMENT,
                "active development executions",
            ),
        )
        for model, task_column, status_column, statuses, label in checks:
            rows = list(
                await self.session.scalars(
                    select(model)
                    .where(task_column.in_(task_ids), status_column.in_(statuses))
                    .with_for_update()
                )
            )
            if rows:
                reasons.append(label)
        agent_ids = {task.assigned_agent_id for task in tasks if task.assigned_agent_id is not None}
        if agent_ids:
            busy_agents = list(
                await self.session.scalars(
                    select(Agent)
                    .where(Agent.id.in_(agent_ids), Agent.status == AgentStatus.BUSY)
                    .with_for_update()
                )
            )
            if busy_agents:
                reasons.append("busy agents")
        return reasons

    async def _task_ids(self, project_id: UUID | None) -> list[UUID]:
        if project_id is None:
            return []
        return list(
            await self.session.scalars(
                select(Task.id).where(Task.project_id == project_id).order_by(Task.id)
            )
        )

    async def _mission_agent_ids(self, mission: Mission) -> list[UUID]:
        if mission.company_id is None:
            return []
        return list(
            await self.session.scalars(
                select(Agent.id).where(
                    Agent.company_id == mission.company_id,
                    Agent.configuration["mission_id"].astext == str(mission.id),
                )
            )
        )

    async def _can_delete_company(
        self, mission: Mission, task_ids: list[UUID], agent_ids: list[UUID]
    ) -> bool:
        if not mission.owns_company or mission.company_id is None:
            return False
        company_id = mission.company_id
        other_missions = await self.session.scalar(
            select(func.count(Mission.id)).where(
                Mission.company_id == company_id, Mission.id != mission.id
            )
        )
        project_filters = [Project.company_id == company_id]
        if mission.project_id is not None:
            project_filters.append(Project.id != mission.project_id)
        other_projects = await self.session.scalar(
            select(func.count(Project.id)).where(*project_filters)
        )
        agent_filters = [Agent.company_id == company_id]
        if agent_ids:
            agent_filters.append(Agent.id.not_in(agent_ids))
        other_agents = await self.session.scalar(select(func.count(Agent.id)).where(*agent_filters))
        task_filters = [Task.company_id == company_id]
        if task_ids:
            task_filters.append(Task.id.not_in(task_ids))
        other_tasks = await self.session.scalar(select(func.count(Task.id)).where(*task_filters))
        return not any((other_missions, other_projects, other_agents, other_tasks))

    async def _delete_topology(
        self,
        mission: Mission,
        task_ids: list[UUID],
        agent_ids: list[UUID],
        delete_company: bool,
    ) -> None:
        await self._delete_linked_events(mission, task_ids, agent_ids, delete_company)
        if task_ids:
            for model in (
                AcceptanceVerification,
                QAResult,
                DevelopmentExecution,
                TaskReview,
                ToolCall,
                ExecutionJob,
                AgentRun,
                TaskRun,
            ):
                await self.session.execute(
                    delete(model)
                    .where(model.task_id.in_(task_ids))
                    .execution_options(synchronize_session=False)
                )
            await self.session.execute(
                delete(TaskDependency)
                .where(
                    or_(
                        TaskDependency.task_id.in_(task_ids),
                        TaskDependency.depends_on_task_id.in_(task_ids),
                    )
                )
                .execution_options(synchronize_session=False)
            )
            await self.session.execute(
                delete(Task)
                .where(Task.id.in_(task_ids))
                .execution_options(synchronize_session=False)
            )
        await self.session.execute(
            delete(PlanningRun)
            .where(PlanningRun.mission_id == mission.id)
            .execution_options(synchronize_session=False)
        )
        mission_id = mission.id
        project_id = mission.project_id
        company_id = mission.company_id
        await self.session.execute(
            delete(Mission)
            .where(Mission.id == mission_id)
            .execution_options(synchronize_session=False)
        )
        if project_id is not None and mission.owns_project:
            await self.session.execute(
                delete(Project)
                .where(Project.id == project_id)
                .execution_options(synchronize_session=False)
            )
        if agent_ids:
            await self.session.execute(
                delete(Agent)
                .where(Agent.id.in_(agent_ids))
                .execution_options(synchronize_session=False)
            )
        if delete_company and company_id is not None:
            await self.session.execute(
                delete(Company)
                .where(Company.id == company_id)
                .execution_options(synchronize_session=False)
            )

    async def _delete_linked_events(
        self,
        mission: Mission,
        task_ids: list[UUID],
        agent_ids: list[UUID],
        delete_company: bool,
    ) -> None:
        conditions = []
        conditions.append(
            and_(
                Event.correlation_id == mission.id,
                Event.type.not_in(("MISSION_DELETION_REQUESTED", "MISSION_DELETED")),
            )
        )
        if mission.project_id is not None:
            conditions.append(Event.project_id == mission.project_id)
        if task_ids:
            conditions.append(Event.task_id.in_(task_ids))
        if agent_ids:
            conditions.append(Event.agent_id.in_(agent_ids))
        if delete_company and mission.company_id is not None:
            conditions.append(Event.company_id == mission.company_id)
        if not conditions:
            return
        event_ids = list(await self.session.scalars(select(Event.id).where(or_(*conditions))))
        if not event_ids:
            return
        for model in (EventConsumption, EventDeadLetter, EventOutbox):
            await self.session.execute(
                delete(model)
                .where(model.event_id.in_(event_ids))
                .execution_options(synchronize_session=False)
            )
        await self.session.execute(
            delete(Event)
            .where(Event.id.in_(event_ids))
            .execution_options(synchronize_session=False)
        )
