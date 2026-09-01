from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql.elements import TextClause

from app.infrastructure import database, redis


@pytest.mark.asyncio
async def test_database_connectivity_executes_select_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = AsyncMock()
    engine = MagicMock()
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=connection)
    context_manager.__aexit__ = AsyncMock(return_value=None)
    engine.connect.return_value = context_manager
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    assert await database.check_database() is True
    statement = connection.execute.await_args.args[0]
    assert isinstance(statement, TextClause)
    assert str(statement) == "SELECT 1"


@pytest.mark.asyncio
async def test_redis_connectivity_sends_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.ping.return_value = True
    monkeypatch.setattr(redis, "get_redis_client", lambda: client)

    assert await redis.check_redis() is True
    client.ping.assert_awaited_once_with()
