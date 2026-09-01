import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.enums import TaskStatus, ToolCallStatus
from app.repositories.task import TaskRepository
from app.repositories.task_run import TaskRunRepository
from app.repositories.tool_call import ToolCallRepository
from app.services.event_factory import EventFactory
from app.services.task_state_machine import TaskStateMachine

logger = logging.getLogger(__name__)


class StaleToolCallRecovery:
    ERROR_CODE = "TOOL_EXECUTION_INTERRUPTED"
    ERROR_MESSAGE = "Tool execution exceeded the stale threshold; side-effect state is unknown"

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.calls = ToolCallRepository(session)
        self.tasks = TaskRepository(session)
        self.task_runs = TaskRunRepository(session)
        self.events = EventFactory(session)

    async def recover(self, limit: int = 100) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.settings.tool_call_stale_seconds)
        calls = await self.calls.claim_stale(cutoff, limit)
        recovered = 0
        for call in calls:
            task = await self.tasks.get(call.task_id)
            if task is None:
                continue
            if task.status == TaskStatus.IN_PROGRESS:
                await TaskStateMachine(self.session).transition(
                    task.id, TaskStatus.FAILED, self.ERROR_MESSAGE, commit=False
                )
            task_run = await self.task_runs.get(call.task_run_id)
            call.status = ToolCallStatus.FAILED
            call.error = {"code": self.ERROR_CODE, "message": self.ERROR_MESSAGE}
            call.completed_at = datetime.now(UTC)
            if task_run is not None:
                task_run.error = {"code": self.ERROR_CODE, "message": self.ERROR_MESSAGE}
            await self.events.create(
                company_id=task.company_id,
                project_id=task.project_id,
                agent_id=call.agent_id,
                task_id=task.id,
                event_type="TOOL_CALL_FAILED",
                message="Stale tool execution was recovered as failed.",
                payload={
                    "tool_call_id": str(call.id),
                    "agent_run_id": str(call.agent_run_id),
                    "task_run_id": str(call.task_run_id),
                    "tool_name": call.tool_name,
                    "status": call.status.value,
                    "error_code": self.ERROR_CODE,
                    "recovered": True,
                },
            )
            recovered += 1
        await self.session.commit()
        logger.info(
            "Stale tool call recovery completed",
            extra={"event": "tool_call_recovery_completed", "recovered": recovered},
        )
        return recovered
