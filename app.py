from __future__ import annotations

import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from backend.api.orders import router as order_router
from backend.rate_limit import RateLimitExceeded, SlidingWindowRateLimiter
from backend.repositories.memory import InMemoryOrderStore
from backend.services.orders import OrderService
from storage.postgres import PostgresOrderStore
from storage.redis_cache import JsonRedisCache
from telemetry import LATENCY, configure_telemetry
from telemetry import REQUESTS as HTTP_REQUESTS

ITEMS: dict[int, dict] = {}
IDEMPOTENCY: dict[str, dict] = {}
RATE_LIMITER = SlidingWindowRateLimiter(limit=30, window_seconds=60)
# Kept as a public alias for backwards-compatible tests and examples.
REQUEST_HISTORY = RATE_LIMITER.history


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: float


def rate_limit(client_id: str) -> None:
    try:
        RATE_LIMITER.check(client_id)
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


def next_id() -> int:
    return max(ITEMS, default=0) + 1


def create_app(
    database_url: str | None = None,
    redis_url: str | None = None,
) -> FastAPI:
    resolved_database_url = database_url or os.getenv("DATABASE_URL")
    resolved_redis_url = redis_url or os.getenv("REDIS_URL")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if resolved_database_url:
            store = await PostgresOrderStore.connect(resolved_database_url)
        else:
            store = InMemoryOrderStore()

        cache: JsonRedisCache | None = None
        if resolved_redis_url:
            redis = Redis.from_url(resolved_redis_url, decode_responses=True)
            cache = JsonRedisCache(redis, prefix="orders", ttl_seconds=60)

        application.state.order_service = OrderService(store, cache)
        application.state.order_cache = cache
        application.state.rate_limit = rate_limit
        try:
            yield
        finally:
            await application.state.order_service.close()
            if cache is not None:
                await cache.close()

    application = FastAPI(
        title="API Backend Engineering Lab",
        version="1.3.0",
        lifespan=lifespan,
    )
    configure_telemetry(application)

    @application.middleware("http")
    async def record_metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/items", status_code=status.HTTP_201_CREATED)
    def create_item(
        payload: ItemCreate,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        x_client_id: Annotated[
            str,
            Header(alias="X-Client-ID"),
        ] = "anonymous",
    ):
        rate_limit(x_client_id)
        body_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        if idempotency_key:
            prior = IDEMPOTENCY.get(idempotency_key)
            if prior:
                if prior["body_hash"] != body_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency key reused with different payload",
                    )
                return prior["response"]

        item_id = next_id()
        response = {"id": item_id, **payload.model_dump()}
        ITEMS[item_id] = response
        if idempotency_key:
            IDEMPOTENCY[idempotency_key] = {
                "body_hash": body_hash,
                "response": response,
            }
        return response

    @application.get("/v1/items")
    def list_items(
        limit: int = Query(20, ge=1, le=100),
        cursor: int = Query(0, ge=0),
    ):
        ids = sorted(item_id for item_id in ITEMS if item_id > cursor)[:limit]
        results = [ITEMS[item_id] for item_id in ids]
        next_cursor = ids[-1] if len(ids) == limit else None
        return {"items": results, "next_cursor": next_cursor}

    application.include_router(order_router)
    return application


app = create_app()
