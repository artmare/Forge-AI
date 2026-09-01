from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EntityNotFoundError
from app.domain.models import Project
from app.repositories.company import CompanyRepository
from app.repositories.project import ProjectRepository
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services.event_factory import EventFactory


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.companies = CompanyRepository(session)
        self.projects = ProjectRepository(session)
        self.events = EventFactory(session)

    async def create(self, company_id: UUID, payload: ProjectCreate) -> Project:
        if await self.companies.get(company_id) is None:
            raise EntityNotFoundError("Company")
        project = Project(company_id=company_id, **payload.model_dump())
        await self.projects.add(project)
        await self.events.create(
            company_id=company_id,
            project_id=project.id,
            event_type="PROJECT_CREATED",
            message=f"Project '{project.name}' was created.",
            payload={"project_id": str(project.id)},
        )
        await self.session.commit()
        return project

    async def get(self, project_id: UUID) -> Project:
        project = await self.projects.get(project_id)
        if project is None:
            raise EntityNotFoundError("Project")
        return project

    async def list_for_company(self, company_id: UUID, offset: int, limit: int) -> list[Project]:
        if await self.companies.get(company_id) is None:
            raise EntityNotFoundError("Company")
        return await self.projects.list_for_company(company_id, offset, limit)

    async def update(self, project_id: UUID, payload: ProjectUpdate) -> Project:
        project = await self.get(project_id)
        old_status = project.status
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(project, field, value)
        if payload.status is not None and payload.status != old_status:
            await self.events.create(
                company_id=project.company_id,
                project_id=project.id,
                event_type="PROJECT_STATUS_CHANGED",
                message=(
                    f"Project status changed from {old_status.value} to {payload.status.value}."
                ),
                payload={"from": old_status.value, "to": payload.status.value},
            )
        await self.session.commit()
        return project
