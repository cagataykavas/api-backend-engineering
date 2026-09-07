from __future__ import annotations

import hashlib
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from orders import (
    IdempotencyConflict,
    InMemoryOrderStore,
    InvalidCursor,
    OrderCreate,
    OrderService,
)
from storage.postgres import PostgresOrderStore
from storage.redis_cache import JsonRedisCache
from telemetry import LATENCY, configure_telemetry
from telemetry import REQUESTS as HTTP_REQUESTS

ITEMS: dict[int, dict] = {}
IDEMPOTENCY: dict[str, dict] = {}
REQUEST_HISTORY: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT = 30
WINDOW_SECONDS = 60


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: float


def rate_limit(client_id: str) -> None:
    now = time.time()
    bucket = REQUEST_HISTORY[client_id]
    while bucket and bucket[0] <= now - WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    bucket.append(now)


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
        try:
            yield
        finally:
            await application.state.order_service.close()
            if cache is not None:
                await cache.close()

    application = FastAPI(
        title="API Backend Engineering Lab",
        version="1.2.0",
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

    @application.post("/v1/orders", status_code=status.HTTP_201_CREATED)
    async def create_order(
        request: Request,
        response: Response,
        payload: OrderCreate,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=200),
        ] = None,
        x_client_id: Annotated[
            str,
            Header(alias="X-Client-ID"),
        ] = "anonymous",
    ):
        rate_limit(x_client_id)
        service: OrderService = request.app.state.order_service
        try:
            order, replayed = await service.create(
                payload,
                idempotency_key=idempotency_key,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
        return order

    @application.get("/v1/orders/{order_id}")
    async def get_order(request: Request, order_id: str):
        service: OrderService = request.app.state.order_service
        order = await service.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    @application.get("/v1/orders")
    async def list_orders(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None),
        customer_id: str | None = Query(None, min_length=1, max_length=80),
    ):
        service: OrderService = request.app.state.order_service
        try:
            return await service.list_orders(
                customer_id=customer_id,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCursor as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return application


app = create_app()
