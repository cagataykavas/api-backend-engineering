from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from orders import IdempotencyConflict, OrderCreate, OrderService
from storage.postgres import PostgresOrderStore
from storage.redis_cache import JsonRedisCache

pytestmark = pytest.mark.skipif(
    os.getenv("INTEGRATION_SERVICES") != "1",
    reason="requires Postgres and Redis integration services",
)


@pytest.mark.asyncio
async def test_postgres_idempotency_and_redis_cache_integration() -> None:
    store = await PostgresOrderStore.connect(
        "postgresql://app:app@127.0.0.1:5432/app"
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    cache = JsonRedisCache(redis, prefix="orders-integration", ttl_seconds=60)
    service = OrderService(store, cache)

    unique = uuid.uuid4().hex
    customer_id = f"integration-{unique}"
    idempotency_key = f"create-{unique}"
    payload = OrderCreate(customer_id=customer_id, amount=42.25)

    try:
        first, second = await asyncio.gather(
            service.create(payload, idempotency_key=idempotency_key),
            service.create(payload, idempotency_key=idempotency_key),
        )
        first_order, first_replayed = first
        second_order, second_replayed = second

        assert first_order == second_order
        assert {first_replayed, second_replayed} == {False, True}

        order_id = str(first_order["order_id"])
        cached = await cache.get(f"order:{order_id}")
        assert cached is not None
        assert cached["customer_id"] == customer_id

        fetched = await service.get(order_id)
        assert fetched == first_order

        page = await service.list_orders(
            customer_id=customer_id,
            limit=10,
            cursor=None,
        )
        assert [order["order_id"] for order in page["orders"]] == [order_id]

        with pytest.raises(IdempotencyConflict):
            await service.create(
                OrderCreate(customer_id=customer_id, amount=99.0),
                idempotency_key=idempotency_key,
            )
    finally:
        await service.close()
        await cache.close()
