import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import AgentRunStatus, TaskStatus
from app.repositories.agent_run import AgentRunRepository
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


class StaleAgentRunRecovery:
    ERROR_CODE = "AGENT_RUNTIME_ERROR"
    ERROR_MESSAGE = "Agent run exceeded the stale execution threshold"

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.agent_runs = AgentRunRepository(session)
        self.tasks = TaskRepository(session)
        self.task_runs = TaskRunRepository(session)
        self.events = EventFactory(session)

    async def recover(self, limit: int = 100) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.agent_run_stale_seconds)
        runs = await self.agent_runs.claim_stale(cutoff, limit)
        recovered = 0
        for run in runs:
            task = await self.tasks.get(run.task_id)
            if task is None:
                continue
            if task.status == TaskStatus.IN_PROGRESS:
                await TaskStateMachine(self.session).transition(
                    task.id,
                    TaskStatus.FAILED,
                    self.ERROR_MESSAGE,
                    commit=False,
                )
            task_run = await self.task_runs.get(run.task_run_id)
            now = datetime.now(UTC)
            run.status = AgentRunStatus.FAILED
            run.error_code = self.ERROR_CODE
            run.error_message = self.ERROR_MESSAGE
            run.completed_at = now
            if task_run is not None:
                task_run.error = {
                    "code": self.ERROR_CODE,
                    "message": self.ERROR_MESSAGE,
                }
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                agent_id=run.agent_id,
                task_id=task.id,
                event_type="AGENT_RUN_FAILED",
                message="Stale agent execution was recovered as failed.",
                payload={
                    "agent_run_id": str(run.id),
                    "task_run_id": str(run.task_run_id),
                    "error_code": self.ERROR_CODE,
                    "recovered": True,
                },
            )
            recovered += 1
        await self.session.commit()
        logger.info(
            "Stale agent run recovery completed",
            extra={"event": "agent_run_recovery_completed", "recovered": recovered},
        )
        return recovered
