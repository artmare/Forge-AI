from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import RuntimeControl


class RuntimeControlRepository:
    AUTONOMY_KEY = "autonomy"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> RuntimeControl | None:
        return await self.session.get(RuntimeControl, self.AUTONOMY_KEY)

    async def get_for_update(self) -> RuntimeControl | None:
        return await self.session.scalar(
            select(RuntimeControl).where(RuntimeControl.key == self.AUTONOMY_KEY).with_for_update()
        )

    async def ensure(self, default_enabled: bool = False) -> RuntimeControl:
        control = await self.get()
        if control is None:
            control = RuntimeControl(key=self.AUTONOMY_KEY, enabled=default_enabled)
            self.session.add(control)
            await self.session.flush()
        return control
