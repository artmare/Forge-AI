import asyncio

from app.infrastructure.database import get_session_factory
from app.tool_system.recovery import StaleToolCallRecovery


async def main() -> None:
    async with get_session_factory()() as session:
        recovered = await StaleToolCallRecovery(session).recover()
        print(f"Recovered {recovered} stale tool call(s).")


if __name__ == "__main__":
    asyncio.run(main())
