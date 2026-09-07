from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    customer_id: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0, le=1_000_000_000)


class IdempotencyConflict(ValueError):
    """Raised when a key is replayed with a different request payload."""


class InvalidCursor(ValueError):
    """Raised when a pagination cursor cannot be decoded safely."""


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    customer_id: str
    amount: float
    status: str
    created_at: datetime


@dataclass(frozen=True)
class CreateOrderResult:
    order: OrderRecord
    replayed: bool


class OrderStore(Protocol):
    async def create(
        self,
        order: OrderRecord,
        *,
        idempotency_key: str | None,
        body_hash: str,
    ) -> CreateOrderResult: ...

    async def get(self, order_id: str) -> OrderRecord | None: ...

    async def list_page(
        self,
        *,
        customer_id: str | None,
        before_created_at: datetime | None,
        before_order_id: str | None,
        limit: int,
    ) -> list[OrderRecord]: ...

    async def close(self) -> None: ...


class JsonCache(Protocol):
    async def get(self, key: str) -> dict | None: ...

    async def set(self, key: str, value: dict) -> None: ...

    async def delete(self, key: str) -> None: ...


class InMemoryOrderStore:
    """Deterministic process-local backend used by unit tests and quick demos."""

    def __init__(self) -> None:
        self._orders: dict[str, OrderRecord] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        order: OrderRecord,
        *,
        idempotency_key: str | None,
        body_hash: str,
    ) -> CreateOrderResult:
        async with self._lock:
            if idempotency_key is not None:
                prior = self._idempotency.get(idempotency_key)
                if prior is not None:
                    prior_hash, prior_order_id = prior
                    if prior_hash != body_hash:
                        raise IdempotencyConflict(
                            "idempotency key reused with different payload"
                        )
                    return CreateOrderResult(self._orders[prior_order_id], replayed=True)

            self._orders[order.order_id] = order
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (body_hash, order.order_id)
            return CreateOrderResult(order, replayed=False)

    async def get(self, order_id: str) -> OrderRecord | None:
        return self._orders.get(order_id)

    async def list_page(
        self,
        *,
        customer_id: str | None,
        before_created_at: datetime | None,
        before_order_id: str | None,
        limit: int,
    ) -> list[OrderRecord]:
        values = list(self._orders.values())
        if customer_id is not None:
            values = [value for value in values if value.customer_id == customer_id]
        values.sort(key=lambda value: (value.created_at, value.order_id), reverse=True)
        if before_created_at is not None and before_order_id is not None:
            boundary = (before_created_at, before_order_id)
            values = [
                value
                for value in values
                if (value.created_at, value.order_id) < boundary
            ]
        return values[:limit]

    async def close(self) -> None:
        return None


def order_to_dict(order: OrderRecord) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "amount": order.amount,
        "status": order.status,
        "created_at": order.created_at.astimezone(timezone.utc).isoformat(),
    }


def canonical_payload_hash(payload: OrderCreate) -> str:
    body = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def encode_cursor(order: OrderRecord) -> str:
    payload = json.dumps(
        {
            "created_at": order.created_at.astimezone(timezone.utc).isoformat(),
            "order_id": order.order_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8")
        )
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        order_id = str(payload["order_id"])
        if not order_id:
            raise ValueError("cursor order id is empty")
        return created_at, order_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursor("invalid pagination cursor") from exc


class OrderService:
    def __init__(self, store: OrderStore, cache: JsonCache | None = None) -> None:
        self.store = store
        self.cache = cache

    async def create(
        self,
        payload: OrderCreate,
        *,
        idempotency_key: str | None,
    ) -> tuple[dict[str, object], bool]:
        order = OrderRecord(
            order_id=str(uuid.uuid4()),
            customer_id=payload.customer_id,
            amount=float(payload.amount),
            status="created",
            created_at=datetime.now(timezone.utc),
        )
        result = await self.store.create(
            order,
            idempotency_key=idempotency_key,
            body_hash=canonical_payload_hash(payload),
        )
        serialized = order_to_dict(result.order)
        if self.cache is not None:
            await self.cache.set(f"order:{result.order.order_id}", serialized)
        return serialized, result.replayed

    async def get(self, order_id: str) -> dict[str, object] | None:
        cache_key = f"order:{order_id}"
        if self.cache is not None:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached

        order = await self.store.get(order_id)
        if order is None:
            return None
        serialized = order_to_dict(order)
        if self.cache is not None:
            await self.cache.set(cache_key, serialized)
        return serialized

    async def list_orders(
        self,
        *,
        customer_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        before_created_at: datetime | None = None
        before_order_id: str | None = None
        if cursor is not None:
            before_created_at, before_order_id = decode_cursor(cursor)

        rows = await self.store.list_page(
            customer_id=customer_id,
            before_created_at=before_created_at,
            before_order_id=before_order_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = encode_cursor(visible[-1]) if has_more and visible else None
        return {
            "orders": [order_to_dict(order) for order in visible],
            "next_cursor": next_cursor,
        }

    async def close(self) -> None:
        await self.store.close()
