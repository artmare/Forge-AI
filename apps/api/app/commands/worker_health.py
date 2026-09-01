import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.core.config import get_settings
from app.domain.enums import WorkerStatus
from app.infrastructure.database import check_database, get_session_factory
from app.repositories.worker_node import WorkerNodeRepository


async def main() -> None:
    path = Path("/tmp/forge-agent-worker-id")
    if not path.is_file() or not await check_database():
        raise SystemExit(1)
    worker_id = UUID(path.read_text(encoding="utf-8").strip())
    async with get_session_factory()() as session:
        worker = await WorkerNodeRepository(session).get(worker_id)
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().worker_stale_seconds)
    if worker is None or worker.status != WorkerStatus.ONLINE or worker.last_heartbeat_at < cutoff:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
