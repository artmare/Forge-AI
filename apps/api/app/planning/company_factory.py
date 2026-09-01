import logging
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import AgentStatus, CompanyStatus, MissionStatus, ProjectStatus, TaskStatus
from app.domain.exceptions import EntityNotFoundError, MissionConflictError
from app.domain.models import Agent, Company, Project, Task, TaskDependency
from app.planning.contracts import PlanProposal, PlanValidationResult
from app.planning.validator import PlanValidator
from app.repositories.company import CompanyRepository
from app.repositories.mission import MissionRepository
from app.repositories.planning_run import PlanningRunRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


class CompanyFactory:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.validator = validator or PlanValidator(self.settings)
        self.missions = MissionRepository(session)
        self.runs = PlanningRunRepository(session)
        self.companies = CompanyRepository(session)
        self.events = EventFactory(session)

    async def activate(self, mission_id: UUID):  # type: ignore[no-untyped-def]
        try:
            mission = await self.missions.get_for_update(mission_id)
            if mission is None:
                raise EntityNotFoundError("Mission")
            if mission.archived_at is not None:
                raise MissionConflictError(
                    "MISSION_ARCHIVED", "Restore the Mission before activating it."
                )
            if mission.project_id is not None and mission.status in {
                MissionStatus.ACTIVE,
                MissionStatus.REVIEW,
                MissionStatus.COMPLETED,
            }:
                return mission
            if mission.status != MissionStatus.PLAN_READY:
                raise MissionConflictError(
                    "MISSION_NOT_ACTIVATABLE",
                    f"Mission in {mission.status.value} cannot be activated.",
                )
            run = await self.runs.latest_for_mission(mission.id, successful_only=True)
            if run is None or run.proposal is None or run.validation_result is None:
                raise MissionConflictError(
                    "MISSION_PLAN_NOT_FOUND", "Mission has no successful validated plan."
                )
            proposal = PlanProposal.model_validate(run.proposal)
            stored_validation = PlanValidationResult.model_validate(run.validation_result)
            current_validation = self.validator.validate(proposal)
            if not stored_validation.valid or not current_validation.valid:
                raise MissionConflictError(
                    "INVALID_PLAN", "Mission plan no longer passes capability validation."
                )

            company = (
                await self.companies.get(mission.company_id)
                if mission.company_id is not None
                else None
            )
            if mission.company_id is not None and company is None:
                raise EntityNotFoundError("Company")
            company_created = company is None
            if company is None:
                company = Company(
                    name=mission.title,
                    slug=self._slug(mission.title, mission.id),
                    goal=mission.goal,
                    description="Created from an explicitly activated Forge Mission.",
                    status=CompanyStatus.ACTIVE,
                )
                self.session.add(company)
                await self.session.flush()
                mission.company_id = company.id
                await self.events.create(
                    event_type="COMPANY_CREATED",
                    message=f"Company '{company.name}' was created from a Mission.",
                    payload={"company_id": str(company.id), "mission_id": str(mission.id)},
                    company_id=company.id,
                    correlation_id=mission.id,
                )

            project = Project(
                company_id=company.id,
                name=proposal.project.name,
                description=proposal.project.description,
                goal=mission.goal,
                status=ProjectStatus.ACTIVE,
            )
            self.session.add(project)
            await self.session.flush()
            await self.events.create(
                event_type="PROJECT_CREATED",
                message=f"Project '{project.name}' was materialized from a Mission plan.",
                payload={"project_id": str(project.id), "mission_id": str(mission.id)},
                company_id=company.id,
                project_id=project.id,
                correlation_id=mission.id,
            )

            agents: dict[str, Agent] = {}
            for proposed in proposal.agents:
                agent = Agent(
                    company_id=company.id,
                    name=proposed.name,
                    role=proposed.role.strip().upper(),
                    status=AgentStatus.IDLE,
                    configuration={
                        "description": proposed.description,
                        "model_alias": proposed.model_alias,
                        "mission_id": str(mission.id),
                        "project_id": str(project.id),
                    },
                    permissions={
                        permission: allowed
                        for permission, allowed in proposed.requested_permissions.items()
                        if allowed
                    },
                )
                self.session.add(agent)
                await self.session.flush()
                agents[proposed.key] = agent
                await self.events.create(
                    event_type="AGENT_CREATED",
                    message=f"Agent '{agent.name}' was materialized from a Mission plan.",
                    payload={
                        "agent_id": str(agent.id),
                        "mission_id": str(mission.id),
                        "role": agent.role,
                    },
                    company_id=company.id,
                    project_id=project.id,
                    agent_id=agent.id,
                    correlation_id=mission.id,
                )

            tasks: dict[str, Task] = {}
            for proposed in proposal.tasks:
                task = Task(
                    company_id=company.id,
                    project_id=project.id,
                    assigned_agent_id=agents[proposed.assigned_agent_key].id,
                    type="MISSION_TASK",
                    kind=proposed.kind,
                    title=proposed.title,
                    description=proposed.description,
                    input=proposed.input,
                    acceptance_criteria=proposed.acceptance_criteria,
                    status=TaskStatus.CREATED,
                    priority=proposed.priority,
                    max_iterations=proposed.max_iterations,
                )
                self.session.add(task)
                await self.session.flush()
                tasks[proposed.key] = task
                await self.events.create(
                    event_type="TASK_CREATED",
                    message=f"Task '{task.title}' was materialized from a Mission plan.",
                    payload={
                        "task_id": str(task.id),
                        "mission_id": str(mission.id),
                        "plan_key": proposed.key,
                    },
                    company_id=company.id,
                    project_id=project.id,
                    agent_id=task.assigned_agent_id,
                    task_id=task.id,
                    correlation_id=mission.id,
                )

            dependent_keys: set[str] = set()
            for proposed in proposal.dependencies:
                task = tasks[proposed.task]
                upstream = tasks[proposed.depends_on]
                if task.project_id != upstream.project_id or task.company_id != upstream.company_id:
                    raise MissionConflictError(
                        "INVALID_PLAN", "Task dependencies must remain in one Project and Company."
                    )
                dependency = TaskDependency(task_id=task.id, depends_on_task_id=upstream.id)
                self.session.add(dependency)
                await self.session.flush()
                dependent_keys.add(proposed.task)
                await self.events.create(
                    event_type="TASK_DEPENDENCY_CREATED",
                    message="Task dependency was materialized.",
                    payload={
                        "task_dependency_id": str(dependency.id),
                        "task_id": str(task.id),
                        "depends_on_task_id": str(upstream.id),
                        "mission_id": str(mission.id),
                    },
                    company_id=company.id,
                    project_id=project.id,
                    task_id=task.id,
                    correlation_id=mission.id,
                )

            state_machine = TaskStateMachine(self.session)
            for key in sorted(set(tasks) - dependent_keys):
                task = await state_machine.transition(
                    tasks[key].id,
                    TaskStatus.QUEUED,
                    "Root Task became ready during Mission activation.",
                    commit=False,
                )
                await self.events.create(
                    event_type="TASK_BECAME_READY",
                    message="Root Task became ready after Mission activation.",
                    payload={"task_id": str(task.id), "mission_id": str(mission.id)},
                    company_id=company.id,
                    project_id=project.id,
                    agent_id=task.assigned_agent_id,
                    task_id=task.id,
                    correlation_id=mission.id,
                )

            now = datetime.now(UTC)
            mission.project_id = project.id
            mission.owns_company = company_created
            mission.owns_project = True
            mission.status = MissionStatus.ACTIVE
            mission.execution_started_at = now
            mission.completed_at = None
            mission.failed_at = None
            mission.failure_reason = None
            await self.events.create(
                event_type="MISSION_ACTIVATED",
                message="Mission plan was activated and materialized.",
                payload={
                    "mission_id": str(mission.id),
                    "company_id": str(company.id),
                    "project_id": str(project.id),
                    "agents_created": len(agents),
                    "tasks_created": len(tasks),
                    "dependencies_created": len(proposal.dependencies),
                },
                company_id=company.id,
                project_id=project.id,
                correlation_id=mission.id,
            )
            await self.session.commit()
            logger.info(
                "Mission topology materialized",
                extra={
                    "event": "mission_factory_completed",
                    "mission_id": str(mission.id),
                    "project_id": str(project.id),
                    "created_agents": len(agents),
                    "created_tasks": len(tasks),
                    "created_dependencies": len(proposal.dependencies),
                },
            )
            return mission
        except IntegrityError as exc:
            await self.session.rollback()
            raise MissionConflictError(
                "PLAN_ACTIVATION_FAILED", "Mission activation conflicted with durable state."
            ) from exc
        except Exception:
            await self.session.rollback()
            raise

    @staticmethod
    def _slug(title: str, mission_id: UUID) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "mission"
        return f"{base[:100].rstrip('-')}-{str(mission_id)[:8]}"
