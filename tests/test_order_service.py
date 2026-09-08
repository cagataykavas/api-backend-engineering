from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.api.schemas import OrderCreate
from backend.domain.orders import OrderRecord, OrderStatus
from backend.errors import IdempotencyConflict, InvalidCursor
from backend.repositories.memory import InMemoryOrderStore
from backend.services.orders import OrderService, decode_cursor, order_to_dict


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}
        self.get_calls = 0

    async def get(self, key: str) -> dict[str, object] | None:
        self.get_calls += 1
        return self.values.get(key)

    async def set(self, key: str, value: dict[str, object]) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def fixed_clock() -> datetime:
    return datetime(2026, 9, 8, 7, 30, tzinfo=timezone.utc)


def id_factory(values: list[str]):
    iterator = iter(values)
    return lambda: next(iterator)


def test_order_record_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OrderRecord(
            order_id="order-1",
            customer_id="customer-1",
            amount=10,
            status=OrderStatus.CREATED,
            created_at=datetime(2026, 9, 8),
        )


@pytest.mark.asyncio
async def test_create_is_deterministic_with_injected_clock_and_id() -> None:
    service = OrderService(
        InMemoryOrderStore(),
        clock=fixed_clock,
        id_factory=id_factory(["order-001"]),
    )

    created, replayed = await service.create(
        OrderCreate(customer_id="customer-1", amount=42.25),
        idempotency_key=None,
    )

    assert replayed is False
    assert created == {
        "order_id": "order-001",
        "customer_id": "customer-1",
        "amount": 42.25,
        "status": "created",
        "created_at": "2026-09-08T07:30:00+00:00",
    }


@pytest.mark.asyncio
async def test_idempotency_replays_original_and_rejects_mutation() -> None:
    service = OrderService(
        InMemoryOrderStore(),
        clock=fixed_clock,
        id_factory=id_factory(["order-001", "order-002", "order-003"]),
    )
    payload = OrderCreate(customer_id="customer-1", amount=42.25)

    first, first_replayed = await service.create(payload, idempotency_key="key-1")
    second, second_replayed = await service.create(payload, idempotency_key="key-1")

    assert first == second
    assert first_replayed is False
    assert second_replayed is True

    with pytest.raises(IdempotencyConflict, match="different payload"):
        await service.create(
            OrderCreate(customer_id="customer-1", amount=43.25),
            idempotency_key="key-1",
        )


@pytest.mark.asyncio
async def test_keyset_pagination_is_stable_for_equal_timestamps() -> None:
    service = OrderService(
        InMemoryOrderStore(),
        clock=fixed_clock,
        id_factory=id_factory(["order-001", "order-002", "order-003"]),
    )
    for amount in (1.0, 2.0, 3.0):
        await service.create(
            OrderCreate(customer_id="customer-1", amount=amount),
            idempotency_key=None,
        )

    first = await service.list_orders(
        customer_id="customer-1",
        limit=2,
        cursor=None,
    )
    assert [row["order_id"] for row in first["orders"]] == [
        "order-003",
        "order-002",
    ]
    assert first["next_cursor"] is not None

    second = await service.list_orders(
        customer_id="customer-1",
        limit=2,
        cursor=str(first["next_cursor"]),
    )
    assert [row["order_id"] for row in second["orders"]] == ["order-001"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_get_uses_cache_after_first_read() -> None:
    store = InMemoryOrderStore()
    cache = MemoryCache()
    service = OrderService(
        store,
        cache,
        clock=fixed_clock,
        id_factory=id_factory(["order-001"]),
    )
    created, _ = await service.create(
        OrderCreate(customer_id="customer-1", amount=10),
        idempotency_key=None,
    )

    # Creation populated the cache; remove the backing row to prove the read is cached.
    store._orders.clear()
    fetched = await service.get("order-001")

    assert fetched == created
    assert cache.get_calls == 1


def test_cursor_decoder_rejects_garbage() -> None:
    with pytest.raises(InvalidCursor, match="invalid pagination cursor"):
        decode_cursor("%%%not-base64-json%%")


def test_order_serialization_uses_enum_value() -> None:
    record = OrderRecord(
        order_id="order-1",
        customer_id="customer-1",
        amount=12.5,
        status=OrderStatus.CANCELLED,
        created_at=fixed_clock(),
    )
    assert order_to_dict(record)["status"] == "cancelled"
