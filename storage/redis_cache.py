from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError


class JsonRedisCache:
    """Small cache-aside adapter that degrades to a miss if Redis is unavailable."""

    def __init__(
        self,
        redis: Redis,
        prefix: str = "api",
        ttl_seconds: int = 60,
    ) -> None:
        self.redis = redis
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self.redis.get(self._key(key))
        except RedisError:
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        try:
            await self.redis.set(
                self._key(key),
                json.dumps(value),
                ex=self.ttl_seconds,
            )
        except RedisError:
            return

    async def delete(self, key: str) -> None:
        try:
            await self.redis.delete(self._key(key))
        except RedisError:
            return

    async def get_or_set(
        self,
        key: str,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await loader()
        await self.set(key, value)
        return value

    async def close(self) -> None:
        await self.redis.aclose()
