import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    pool_options = (
        {"poolclass": NullPool}
        if settings.app_env == "test"
        else {
            "pool_size": 5,
            "max_overflow": 10,
        }
    )
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        **pool_options,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


async def check_database() -> bool:
    """Run a real minimal query against PostgreSQL."""

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError) as exc:
        logger.warning(
            "PostgreSQL health check failed",
            extra={
                "event": "dependency_health_failed",
                "dependency": "postgresql",
                "error_type": type(exc).__name__,
            },
        )
        return False


async def close_database() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
        get_session_factory.cache_clear()
        get_engine.cache_clear()
