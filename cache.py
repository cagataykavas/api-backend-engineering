from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol


class Cache(Protocol):
    def get(self, key: str) -> str | None: ...
    def setex(self, key: str, ttl: int, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


@dataclass
class MemoryCache:
    values: dict[str, tuple[float, str]]

    def __init__(self) -> None:
        self.values = {}

    def get(self, key: str) -> str | None:
        item = self.values.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.time() >= expires_at:
            self.values.pop(key, None)
            return None
        return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = (time.time() + ttl, value)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class UserRepository:
    def __init__(self, cache: Cache, loader, ttl_seconds: int = 60) -> None:
        self.cache = cache
        self.loader = loader
        self.ttl_seconds = ttl_seconds

    def get(self, user_id: str) -> dict[str, Any] | None:
        key = f"user:{user_id}"
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        user = self.loader(user_id)
        if user is not None:
            self.cache.setex(key, self.ttl_seconds, json.dumps(user))
        return user

    def invalidate(self, user_id: str) -> None:
        self.cache.delete(f"user:{user_id}")
