from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import (
    AgentStatus,
    ExecutionJobStatus,
    MissionStatus,
    PlanningRunStatus,
    ProjectStatus,
    TaskStatus,
)
from app.domain.exceptions import EntityNotFoundError, MissionConflictError
from app.domain.models import Agent, ExecutionJob, Mission, PlanningRun, Project, Task
from app.repositories.company import CompanyRepository
from app.repositories.mission import MissionRepository
from app.repositories.planning_run import PlanningRunRepository
from app.schemas.mission import MissionCreate, MissionProgress
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine


class MissionService:
    TERMINAL = frozenset({MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED})

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.missions = MissionRepository(session)
        self.runs = PlanningRunRepository(session)
        self.companies = CompanyRepository(session)
        self.events = EventFactory(session)

    async def create(self, payload: MissionCreate) -> Mission:
        if payload.company_id is not None and await self.companies.get(payload.company_id) is None:
            raise EntityNotFoundError("Company")
        mission = Mission(
            **payload.model_dump(),
            status=MissionStatus.DRAFT,
            planning_attempts=0,
            max_planning_attempts=self.settings.mission_max_planning_attempts,
        )
        await self.missions.add(mission)
        await self.events.create(
            event_type="MISSION_CREATED",
            message=f"Mission '{mission.title}' was created from a user-provided goal.",
            payload={"mission_id": str(mission.id)},
            company_id=mission.company_id,
            correlation_id=mission.id,
        )
        await self.session.commit()
        return mission

    async def get(self, mission_id: UUID) -> Mission:
        mission = await self.missions.get(mission_id)
        if mission is None:
            raise EntityNotFoundError("Mission")
        return mission

    async def list(
        self,
        *,
        company_id: UUID | None,
        status: MissionStatus | None,
        archive: Literal["active", "archived", "all"],
        offset: int,
        limit: int,
    ) -> list[Mission]:
        return await self.missions.list(
            company_id=company_id,
            status=status,
            archive=archive,
            offset=offset,
            limit=limit,
        )

    async def project_name(self, mission: Mission) -> str | None:
        if mission.project_id is None:
            return None
        return await self.session.scalar(
            select(Project.name).where(Project.id == mission.project_id)
        )

    async def progress(self, mission: Mission) -> MissionProgress:
        if mission.project_id is None:
            return MissionProgress()
        rows = await self.session.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.project_id == mission.project_id)
            .group_by(Task.status)
        )
        counts = {status: int(count) for status, count in rows}
        return MissionProgress(
            total=sum(counts.values()),
            done=counts.get(TaskStatus.DONE, 0),
            review=counts.get(TaskStatus.REVIEW, 0),
            running=counts.get(TaskStatus.IN_PROGRESS, 0),
            queued=counts.get(TaskStatus.QUEUED, 0),
            blocked=counts.get(TaskStatus.CREATED, 0),
            failed=counts.get(TaskStatus.FAILED, 0),
            cancelled=counts.get(TaskStatus.CANCELLED, 0),
        )

    async def latest_plan(self, mission_id: UUID) -> PlanningRun:
        if await self.missions.get(mission_id) is None:
            raise EntityNotFoundError("Mission")
        run = await self.runs.latest_for_mission(mission_id)
        if run is None:
            raise EntityNotFoundError("Mission plan")
        return run

    async def cancel(self, mission_id: UUID) -> Mission:
        try:
            mission = await self.missions.get_for_update(mission_id)
            if mission is None:
                raise EntityNotFoundError("Mission")
            if mission.archived_at is not None:
                raise MissionConflictError(
                    "MISSION_ARCHIVED", "Restore the Mission before cancelling it."
                )
            if mission.status == MissionStatus.CANCELLED:
                return mission
            if mission.status == MissionStatus.COMPLETED:
                raise MissionConflictError(
                    "MISSION_NOT_CANCELLABLE", "A completed Mission cannot be cancelled."
                )
            now = datetime.now(UTC)
            if mission.status == MissionStatus.PLANNING:
                active_runs = list(
                    await self.session.scalars(
                        select(PlanningRun)
                        .where(
                            PlanningRun.mission_id == mission.id,
                            PlanningRun.status.in_(
                                (PlanningRunStatus.CREATED, PlanningRunStatus.RUNNING)
                            ),
                        )
                        .with_for_update()
                    )
                )
                for run in active_runs:
                    run.status = PlanningRunStatus.CANCELLED
                    run.completed_at = now
                    run.error = {
                        "code": "MISSION_CANCELLED",
                        "message": "Mission was cancelled during planning.",
                    }

            if mission.project_id is not None:
                project = await self.session.scalar(
                    select(Project).where(Project.id == mission.project_id).with_for_update()
                )
                tasks = list(
                    await self.session.scalars(
                        select(Task)
                        .where(
                            Task.project_id == mission.project_id,
                            Task.status.not_in(
                                (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)
                            ),
                        )
                        .order_by(Task.id)
                        .with_for_update()
                    )
                )
                state_machine = TaskStateMachine(self.session)
                for task in tasks:
                    await state_machine.transition(
                        task.id,
                        TaskStatus.CANCELLED,
                        "Parent Mission was cancelled.",
                        commit=False,
                    )
                jobs = list(
                    await self.session.scalars(
                        select(ExecutionJob)
                        .join(Task, Task.id == ExecutionJob.task_id)
                        .where(
                            Task.project_id == mission.project_id,
                            ExecutionJob.status.in_(
                                (
                                    ExecutionJobStatus.PENDING,
                                    ExecutionJobStatus.CLAIMED,
                                    ExecutionJobStatus.RUNNING,
                                )
                            ),
                        )
                        .order_by(ExecutionJob.id)
                        .with_for_update(of=ExecutionJob)
                    )
                )
                affected_agents: set[UUID] = set()
                for job in jobs:
                    affected_agents.add(job.agent_id)
                    job.status = ExecutionJobStatus.CANCELLED
                    job.completed_at = now
                    job.last_error = {
                        "code": "MISSION_CANCELLED",
                        "message": "Mission cancellation stopped this execution job.",
                    }
                for agent_id in sorted(affected_agents):
                    agent = await self.session.scalar(
                        select(Agent).where(Agent.id == agent_id).with_for_update()
                    )
                    if agent is not None and agent.status == AgentStatus.BUSY:
                        agent.status = AgentStatus.IDLE
                if project is not None and project.status != ProjectStatus.COMPLETED:
                    project.status = ProjectStatus.CANCELLED

            mission.status = MissionStatus.CANCELLED
            mission.completed_at = now
            mission.failure_reason = None
            await self.events.create(
                event_type="MISSION_CANCELLED",
                message="Mission was cancelled without deleting history.",
                payload={
                    "mission_id": str(mission.id),
                    "project_id": (str(mission.project_id) if mission.project_id else None),
                },
                company_id=mission.company_id,
                project_id=mission.project_id,
                correlation_id=mission.id,
            )
            await self.session.commit()
            return mission
        except Exception:
            await self.session.rollback()
            raise
