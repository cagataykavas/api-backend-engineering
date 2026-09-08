from __future__ import annotations

import asyncio
from datetime import datetime

from backend.domain.orders import CreateOrderResult, OrderRecord
from backend.errors import IdempotencyConflict


class InMemoryOrderStore:
    """Deterministic process-local repository used by unit tests and demos."""

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
                    return CreateOrderResult(
                        self._orders[prior_order_id],
                        replayed=True,
                    )

            self._orders[order.order_id] = order
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = (
                    body_hash,
                    order.order_id,
                )
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
            values = [
                value for value in values if value.customer_id == customer_id
            ]
        values.sort(
            key=lambda value: (value.created_at, value.order_id),
            reverse=True,
        )
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
