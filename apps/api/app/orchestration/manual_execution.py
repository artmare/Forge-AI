from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentStatus, ExecutionJobStatus, TaskStatus
from app.domain.exceptions import EntityNotFoundError, ExecutionConflictError
from app.domain.models import Agent, ExecutionJob
from app.repositories.execution_job import ExecutionJobRepository
from app.repositories.runtime_control import RuntimeControlRepository
from app.repositories.task import TaskRepository


class ManualExecutionGuard:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.controls = RuntimeControlRepository(session)
        self.tasks = TaskRepository(session)

    async def acquire(self, task_id: UUID) -> UUID:
        try:
            await self.controls.get_for_update()
            task = await self.tasks.get_for_update(task_id)
            if task is None:
                raise EntityNotFoundError("Task")
            if task.status != TaskStatus.QUEUED or task.assigned_agent_id is None:
                raise ExecutionConflictError("Task is not eligible for manual execution.")
            active_jobs = list(
                await self.session.scalars(
                    select(ExecutionJob)
                    .where(
                        ExecutionJob.task_id == task.id,
                        ExecutionJob.status.in_(ExecutionJobRepository.ACTIVE_STATUSES),
                    )
                    .with_for_update()
                )
            )
            if any(
                job.status in (ExecutionJobStatus.CLAIMED, ExecutionJobStatus.RUNNING)
                for job in active_jobs
            ):
                raise ExecutionConflictError("An autonomous worker already owns this task.")
            now = datetime.now(UTC)
            for job in active_jobs:
                job.status = ExecutionJobStatus.CANCELLED
                job.completed_at = now
                job.last_error = {
                    "code": "MANUAL_EXECUTION_SELECTED",
                    "message": "Pending autonomous job was superseded by manual execution.",
                }
            agent = await self.session.scalar(
                select(Agent).where(Agent.id == task.assigned_agent_id).with_for_update()
            )
            if agent is None or agent.status not in (AgentStatus.CREATED, AgentStatus.IDLE):
                raise ExecutionConflictError("Assigned agent is not available for execution.")
            agent.status = AgentStatus.BUSY
            await self.session.commit()
            return agent.id
        except Exception:
            await self.session.rollback()
            raise

    async def release(self, agent_id: UUID) -> None:
        agent = await self.session.scalar(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
        if agent is not None and agent.status == AgentStatus.BUSY:
            agent.status = AgentStatus.IDLE
        await self.session.commit()
