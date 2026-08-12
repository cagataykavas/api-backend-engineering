from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    amount: float
    status: str


class OrderRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "OrderRepository":
        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10, command_timeout=10)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
                    status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_orders_customer_created
                  ON orders(customer_id, created_at DESC);
                """
            )
        return cls(pool)

    async def create(self, order: Order) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO orders(order_id, customer_id, amount, status) VALUES($1, $2, $3, $4)",
                    order.order_id,
                    order.customer_id,
                    order.amount,
                    order.status,
                )

    async def get(self, order_id: str) -> Order | None:
        row = await self.pool.fetchrow(
            "SELECT order_id, customer_id, amount, status FROM orders WHERE order_id=$1",
            order_id,
        )
        if row is None:
            return None
        return Order(row["order_id"], row["customer_id"], float(row["amount"]), row["status"])

    async def list_for_customer(self, customer_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT order_id, customer_id, amount, status, created_at
            FROM orders
            WHERE customer_id=$1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            customer_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def close(self) -> None:
        await self.pool.close()
