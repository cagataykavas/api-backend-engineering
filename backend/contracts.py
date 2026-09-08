from __future__ import annotations

from datetime import datetime
from typing import Protocol

from backend.domain.orders import CreateOrderResult, OrderRecord


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
    async def get(self, key: str) -> dict[str, object] | None: ...

    async def set(self, key: str, value: dict[str, object]) -> None: ...

    async def delete(self, key: str) -> None: ...
