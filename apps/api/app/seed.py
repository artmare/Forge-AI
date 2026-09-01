import asyncio

from app.domain.enums import TaskStatus
from app.infrastructure.database import get_session_factory
from app.repositories.company import CompanyRepository
from app.schemas.agent import AgentCreate
from app.schemas.company import CompanyCreate
from app.schemas.project import ProjectCreate
from app.schemas.task import TaskCreate
from app.services.agent import AgentService
from app.services.company import CompanyService
from app.services.project import ProjectService
from app.services.task import TaskService
from app.services.task_state_machine import TaskStateMachine


async def seed() -> None:
    async with get_session_factory()() as session:
        existing = await CompanyRepository(session).get_by_slug("forge-demo")
        if existing is not None:
            print("Development seed already exists; no changes made.")
            return

        company = await CompanyService(session).create(
            CompanyCreate(
                name="Forge Demo Company",
                slug="forge-demo",
                goal="Demonstrate the Phase 03 task lifecycle",
                description="Explicit development seed data",
            )
        )
        project = await ProjectService(session).create(
            company.id,
            ProjectCreate(
                name="Launch Project",
                goal="Prepare a sample product launch",
                description="Seeded project",
            ),
        )
        developer = await AgentService(session).create(
            company.id,
            AgentCreate(name="Developer 1", role="DEVELOPER"),
        )
        reviewer = await AgentService(session).create(
            company.id,
            AgentCreate(name="Reviewer 1", role="REVIEWER"),
        )
        implementation = await TaskService(session).create(
            TaskCreate(
                company_id=company.id,
                project_id=project.id,
                assigned_agent_id=developer.id,
                type="IMPLEMENTATION",
                title="Build sample homepage",
                acceptance_criteria=["Responsive layout", "No console errors"],
                max_iterations=3,
            )
        )
        review = await TaskService(session).create(
            TaskCreate(
                company_id=company.id,
                project_id=project.id,
                assigned_agent_id=reviewer.id,
                type="REVIEW",
                title="Review sample homepage",
                acceptance_criteria=["Acceptance criteria verified"],
                max_iterations=2,
            )
        )
        state_machine = TaskStateMachine(session)
        await state_machine.transition(implementation.id, TaskStatus.QUEUED, "Seed task is ready")
        await state_machine.transition(
            implementation.id, TaskStatus.IN_PROGRESS, "Seed execution started"
        )
        await state_machine.transition(
            implementation.id, TaskStatus.REVIEW, "Seed execution completed"
        )
        await state_machine.transition(implementation.id, TaskStatus.DONE, "Seed result accepted")
        await state_machine.transition(review.id, TaskStatus.QUEUED, "Seed review is queued")
        print("Development seed created with Phase 03 lifecycle history.")


if __name__ == "__main__":
    asyncio.run(seed())
