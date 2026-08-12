from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


class JsonRedisCache:
    def __init__(self, redis: Redis, prefix: str = "api", ttl_seconds: int = 60):
        self.redis = redis
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._key(key))
        return None if raw is None else json.loads(raw)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        await self.redis.set(self._key(key), json.dumps(value), ex=self.ttl_seconds)

    async def delete(self, key: str) -> None:
        await self.redis.delete(self._key(key))

    async def get_or_set(self, key: str, loader) -> dict[str, Any]:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await loader()
        await self.set(key, value)
        return value
