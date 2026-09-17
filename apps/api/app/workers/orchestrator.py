import asyncio
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.database import get_session_factory
from app.orchestration.orchestrator import OrchestratorService
from app.orchestration.recovery import OrchestrationRecoveryService
from app.planning.dependency_resolver import DependencyResolver
from app.planning.mission_planner import MissionPlanner
from app.schemas.event_system import EventEnvelope
from app.workers.event_consumer import EventConsumer

settings = get_settings()
configure_logging(settings.log_level, "forge-orchestrator")
logger = logging.getLogger(__name__)


async def handle_orchestration_event(envelope: EventEnvelope) -> None:
    if envelope.event_type != "TASK_STATUS_CHANGED" or envelope.task_id is None:
        return
    target = envelope.payload.get("to_status")
    if target == "QUEUED":
        async with get_session_factory()() as session:
            await OrchestratorService(session).schedule_task(
                envelope.task_id,
                causation_id=envelope.event_id,
                correlation_id=envelope.correlation_id,
            )
    elif target == "DONE":
        async with get_session_factory()() as session:
            resolver = DependencyResolver(session)
            task = await resolver.tasks.get(envelope.task_id)
            await resolver.resolve_dependents(
                envelope.task_id, causation_id=envelope.event_id, commit=False
            )
            if task is not None and task.project_id is not None:
                await resolver.reconcile_project(task.project_id, commit=False)
            await session.commit()
    elif target in {"FAILED", "CANCELLED"}:
        async with get_session_factory()() as session:
            resolver = DependencyResolver(session)
            task = await resolver.tasks.get(envelope.task_id)
            await resolver.resolve_terminal_dependents(
                envelope.task_id, causation_id=envelope.event_id, commit=False
            )
            if task is not None and task.project_id is not None:
                await resolver.reconcile_project(task.project_id, commit=False)
            await session.commit()


async def reconciliation_loop() -> None:
    while True:
        try:
            async with get_session_factory()() as session:
                await DependencyResolver(session).reconcile()
            async with get_session_factory()() as session:
                await OrchestratorService(session).reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Orchestrator reconciliation failed",
                extra={"event": "orchestrator_reconciliation_failed"},
            )
        await asyncio.sleep(settings.orchestrator_reconcile_interval_seconds)


async def recovery_loop() -> None:
    while True:
        try:
            async with get_session_factory()() as session:
                await OrchestrationRecoveryService(session).recover()
            async with get_session_factory()() as session:
                await MissionPlanner(session).recover_stale()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Orchestrator recovery failed",
                extra={"event": "orchestrator_recovery_failed"},
            )
        await asyncio.sleep(settings.orchestrator_recovery_interval_seconds)


async def main() -> None:
    await asyncio.gather(
        EventConsumer(
            name="orchestrator",
            handler=handle_orchestration_event,
            subscribed_topics=frozenset({"forge.task"}),
        ).run(),
        reconciliation_loop(),
        recovery_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
