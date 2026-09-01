import json
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import get_settings
from app.infrastructure.redis import get_redis_client


class RedisEventStream:
    def __init__(self, redis: Redis | None = None) -> None:
        self.redis = redis or get_redis_client()
        self.settings = get_settings()

    async def publish(self, topic: str, envelope: dict[str, Any]) -> str:
        return await self.redis.xadd(
            self.settings.event_stream_name,
            {
                "event_id": envelope["event_id"],
                "event_type": envelope["event_type"],
                "topic": topic,
                "envelope": json.dumps(envelope, separators=(",", ":")),
            },
            maxlen=self.settings.event_stream_maxlen,
            approximate=True,
        )

    async def ensure_group(self, group: str) -> None:
        try:
            await self.redis.xgroup_create(
                self.settings.event_stream_name, group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_group(
        self, group: str, consumer: str, count: int
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        return await self.redis.xreadgroup(
            group,
            consumer,
            {self.settings.event_stream_name: ">"},
            count=count,
            block=self.settings.event_consumer_block_ms,
        )

    async def reclaim_stale(
        self, group: str, consumer: str, count: int
    ) -> list[tuple[str, dict[str, str]]]:
        response = await self.redis.xautoclaim(
            self.settings.event_stream_name,
            group,
            consumer,
            min_idle_time=self.settings.event_processing_lease_seconds * 1_000,
            start_id="0-0",
            count=count,
        )
        return response[1] if response else []

    async def acknowledge(self, group: str, message_id: str) -> None:
        await self.redis.xack(self.settings.event_stream_name, group, message_id)

    async def dead_letter(
        self,
        *,
        consumer: str,
        source_message_id: str,
        envelope: dict[str, Any],
        reason: str,
        attempts: int,
    ) -> str:
        return await self.redis.xadd(
            self.settings.event_dlq_stream_name,
            {
                "consumer": consumer,
                "source_message_id": source_message_id,
                "event_id": envelope["event_id"],
                "event_type": envelope["event_type"],
                "reason": reason,
                "attempts": attempts,
                "envelope": json.dumps(envelope, separators=(",", ":")),
            },
            maxlen=self.settings.event_stream_maxlen,
            approximate=True,
        )

    async def read(self, last_id: str, count: int = 100, block_ms: int = 15_000):
        return await self.redis.xread(
            {self.settings.event_stream_name: last_id}, count=count, block=block_ms
        )
