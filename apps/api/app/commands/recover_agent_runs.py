import asyncio

from app.agent_runtime.recovery import StaleAgentRunRecovery
from app.infrastructure.database import get_session_factory


async def main() -> None:
    async with get_session_factory()() as session:
        recovered = await StaleAgentRunRecovery(session).recover()
        print(f"Recovered {recovered} stale agent run(s).")


if __name__ == "__main__":
    asyncio.run(main())
