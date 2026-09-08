from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from backend.contracts import JsonCache, OrderStore
from backend.domain.orders import OrderRecord, OrderStatus
from backend.errors import InvalidCursor


class OrderCreateInput(Protocol):
    customer_id: str
    amount: float


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_order_id() -> str:
    return str(uuid.uuid4())


def order_to_dict(order: OrderRecord) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "amount": order.amount,
        "status": order.status.value,
        "created_at": order.created_at.astimezone(UTC).isoformat(),
    }


def canonical_payload_hash(payload: OrderCreateInput) -> str:
    body = json.dumps(
        {
            "amount": float(payload.amount),
            "customer_id": payload.customer_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def encode_cursor(order: OrderRecord) -> str:
    payload = json.dumps(
        {
            "created_at": order.created_at.astimezone(UTC).isoformat(),
            "order_id": order.order_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        order_id = str(payload["order_id"])
        if not order_id:
            raise ValueError("cursor order id is empty")
        return created_at, order_id
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidCursor("invalid pagination cursor") from exc


class OrderService:
    """Application service for order creation, lookup and cursor pagination."""

    def __init__(
        self,
        store: OrderStore,
        cache: JsonCache | None = None,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_order_id,
    ) -> None:
        self.store = store
        self.cache = cache
        self._clock = clock
        self._id_factory = id_factory

    async def create(
        self,
        payload: OrderCreateInput,
        *,
        idempotency_key: str | None,
    ) -> tuple[dict[str, object], bool]:
        order = OrderRecord(
            order_id=self._id_factory(),
            customer_id=payload.customer_id,
            amount=float(payload.amount),
            status=OrderStatus.CREATED,
            created_at=self._clock(),
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
