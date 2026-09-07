from __future__ import annotations

from datetime import datetime

import asyncpg

from orders import CreateOrderResult, IdempotencyConflict, OrderRecord


class PostgresOrderStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> PostgresOrderStore:
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=2,
            max_size=10,
            command_timeout=10,
        )
        async with pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
                    status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_orders_created
                    ON orders(created_at DESC, order_id DESC);

                CREATE INDEX IF NOT EXISTS idx_orders_customer_created
                    ON orders(customer_id, created_at DESC, order_id DESC);

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    body_hash TEXT NOT NULL,
                    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        return cls(pool)

    @staticmethod
    def _from_row(row: asyncpg.Record) -> OrderRecord:
        return OrderRecord(
            order_id=str(row["order_id"]),
            customer_id=str(row["customer_id"]),
            amount=float(row["amount"]),
            status=str(row["status"]),
            created_at=row["created_at"],
        )

    async def create(
        self,
        order: OrderRecord,
        *,
        idempotency_key: str | None,
        body_hash: str,
    ) -> CreateOrderResult:
        async with self.pool.acquire() as connection, connection.transaction():
            if idempotency_key is not None:
                # Transaction-scoped advisory locking serializes concurrent requests
                # that reuse the same idempotency key without creating a global lock.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    idempotency_key,
                )
                prior = await connection.fetchrow(
                    """
                    SELECT i.body_hash, o.order_id, o.customer_id, o.amount,
                           o.status, o.created_at
                    FROM idempotency_keys AS i
                    JOIN orders AS o ON o.order_id = i.order_id
                    WHERE i.key = $1
                    """,
                    idempotency_key,
                )
                if prior is not None:
                    if str(prior["body_hash"]) != body_hash:
                        raise IdempotencyConflict(
                            "idempotency key reused with different payload"
                        )
                    return CreateOrderResult(
                        self._from_row(prior),
                        replayed=True,
                    )

            await connection.execute(
                """
                INSERT INTO orders(
                    order_id, customer_id, amount, status, created_at
                ) VALUES($1, $2, $3, $4, $5)
                """,
                order.order_id,
                order.customer_id,
                order.amount,
                order.status,
                order.created_at,
            )
            if idempotency_key is not None:
                await connection.execute(
                    """
                    INSERT INTO idempotency_keys(key, body_hash, order_id)
                    VALUES($1, $2, $3)
                    """,
                    idempotency_key,
                    body_hash,
                    order.order_id,
                )
            return CreateOrderResult(order, replayed=False)

    async def get(self, order_id: str) -> OrderRecord | None:
        row = await self.pool.fetchrow(
            """
            SELECT order_id, customer_id, amount, status, created_at
            FROM orders
            WHERE order_id = $1
            """,
            order_id,
        )
        return None if row is None else self._from_row(row)

    async def list_page(
        self,
        *,
        customer_id: str | None,
        before_created_at: datetime | None,
        before_order_id: str | None,
        limit: int,
    ) -> list[OrderRecord]:
        conditions: list[str] = []
        arguments: list[object] = []

        if customer_id is not None:
            arguments.append(customer_id)
            conditions.append(f"customer_id = ${len(arguments)}")

        if before_created_at is not None and before_order_id is not None:
            arguments.extend([before_created_at, before_order_id])
            timestamp_index = len(arguments) - 1
            order_id_index = len(arguments)
            conditions.append(
                f"(created_at, order_id) < (${timestamp_index}, ${order_id_index})"
            )

        arguments.append(limit)
        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        query = f"""
            SELECT order_id, customer_id, amount, status, created_at
            FROM orders
            WHERE {where_clause}
            ORDER BY created_at DESC, order_id DESC
            LIMIT ${len(arguments)}
        """
        rows = await self.pool.fetch(query, *arguments)
        return [self._from_row(row) for row in rows]

    async def close(self) -> None:
        await self.pool.close()


# Compatibility with the earlier portfolio iteration.
OrderRepository = PostgresOrderStore
