import logging
from asyncio import AbstractEventLoop, get_running_loop
from weakref import WeakKeyDictionary

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_clients: WeakKeyDictionary[AbstractEventLoop, Redis] = WeakKeyDictionary()


def get_redis_client() -> Redis:
    """Return a client scoped to the active event loop.

    Redis connections own asyncio transports, so sharing one cached client
    across loops is unsafe (and is common in async test runners).
    """

    loop = get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = Redis.from_url(get_settings().redis_url, decode_responses=True)
        _clients[loop] = client
    return client


async def check_redis() -> bool:
    """Issue a real PING against Redis."""

    try:
        return bool(await get_redis_client().ping())
    except (RedisError, OSError) as exc:
        logger.warning(
            "Redis health check failed",
            extra={
                "event": "dependency_health_failed",
                "dependency": "redis",
                "error_type": type(exc).__name__,
            },
        )
        return False


async def close_redis() -> None:
    loop = get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None:
        await client.aclose()
