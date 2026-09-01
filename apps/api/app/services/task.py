from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TaskStatus
from app.domain.exceptions import (
    EntityNotFoundError,
    InvalidRelationshipError,
    InvalidTaskInputError,
)
from app.domain.models import Task
from app.repositories.agent import AgentRepository
from app.repositories.company import CompanyRepository
from app.repositories.project import ProjectRepository
from app.repositories.task import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.event_factory import EventFactory


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.companies = CompanyRepository(session)
        self.projects = ProjectRepository(session)
        self.agents = AgentRepository(session)
        self.tasks = TaskRepository(session)
        self.events = EventFactory(session)

    async def _validate_relationships(
        self,
        company_id: UUID,
        project_id: UUID | None,
        agent_id: UUID | None,
        parent_task_id: UUID | None,
        current_task_id: UUID | None = None,
    ) -> None:
        if await self.companies.get(company_id) is None:
            raise EntityNotFoundError("Company")
        if project_id is not None:
            project = await self.projects.get(project_id)
            if project is None:
                raise EntityNotFoundError("Project")
            if project.company_id != company_id:
                raise InvalidRelationshipError("Project must belong to the task company.")
        if agent_id is not None:
            agent = await self.agents.get(agent_id)
            if agent is None:
                raise EntityNotFoundError("Agent")
            if agent.company_id != company_id:
                raise InvalidRelationshipError("Assigned agent must belong to the task company.")
        if parent_task_id is not None:
            if parent_task_id == current_task_id:
                raise InvalidRelationshipError("A task cannot be its own parent.")
            parent = await self.tasks.get(parent_task_id)
            if parent is None:
                raise EntityNotFoundError("Parent task")
            if parent.company_id != company_id:
                raise InvalidRelationshipError("Parent task must belong to the task company.")
            if project_id is not None and parent.project_id != project_id:
                raise InvalidRelationshipError(
                    "Parent task must belong to the same project when the task has a project."
                )

    async def create(self, payload: TaskCreate) -> Task:
        await self._validate_relationships(
            payload.company_id,
            payload.project_id,
            payload.assigned_agent_id,
            payload.parent_task_id,
        )
        task = Task(**payload.model_dump())
        await self.tasks.add(task)
        await self.events.create(
            company_id=task.company_id,
            project_id=task.project_id,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            event_type="TASK_CREATED",
            message=f"Task '{task.title}' was created.",
            payload={"task_id": str(task.id), "type": task.type},
        )
        await self.session.commit()
        return task

    async def get(self, task_id: UUID) -> Task:
        task = await self.tasks.get(task_id)
        if task is None:
            raise EntityNotFoundError("Task")
        return task

    async def list(
        self,
        *,
        company_id: UUID | None,
        project_id: UUID | None,
        assigned_agent_id: UUID | None,
        status: TaskStatus | None,
        offset: int,
        limit: int,
    ) -> list[Task]:
        return await self.tasks.list(
            company_id=company_id,
            project_id=project_id,
            assigned_agent_id=assigned_agent_id,
            status=status,
            offset=offset,
            limit=limit,
        )

    async def update(self, task_id: UUID, payload: TaskUpdate) -> Task:
        task = await self.tasks.get_for_update(task_id)
        if task is None:
            raise EntityNotFoundError("Task")
        changes = payload.model_dump(exclude_unset=True)
        await self._validate_relationships(
            task.company_id,
            changes.get("project_id", task.project_id),
            changes.get("assigned_agent_id", task.assigned_agent_id),
            changes.get("parent_task_id", task.parent_task_id),
            task.id,
        )
        if changes.get("max_iterations", task.max_iterations) < task.iteration:
            raise InvalidTaskInputError(
                "max_iterations cannot be lower than the task's completed execution attempts."
            )
        for field, value in changes.items():
            setattr(task, field, value)
        await self.session.commit()
        return task
