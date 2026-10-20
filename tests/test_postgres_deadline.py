from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import pytest

import storage.postgres as postgres_module
from storage.postgres import PostgresOrderStore


class FakeTransaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: object, **kwargs: object) -> str:
        self.executions.append((query, args, kwargs))
        return "SELECT 1"


class FakeAcquire(AbstractAsyncContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquire_timeouts: list[float | None] = []

    def acquire(self, *, timeout: float | None = None) -> FakeAcquire:
        self.acquire_timeouts.append(timeout)
        return FakeAcquire(self.connection)


@pytest.mark.asyncio
async def test_request_transaction_applies_remaining_budget_to_pool_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = iter([0.200, 0.1499])
    monkeypatch.setattr(
        postgres_module,
        "remaining_request_seconds",
        lambda *, reserve_ms: next(budgets),
    )
    pool = FakePool()
    store = PostgresOrderStore(pool)  # type: ignore[arg-type]

    async with store._request_transaction() as connection:
        assert connection is pool.connection

    assert pool.acquire_timeouts == [0.200]
    assert pool.connection.executions == [
        (
            "SELECT set_config('statement_timeout', $1, true)",
            ("149ms",),
            {"timeout": 0.1499},
        )
    ]


@pytest.mark.asyncio
async def test_request_transaction_does_not_override_pool_policy_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        postgres_module,
        "remaining_request_seconds",
        lambda *, reserve_ms: None,
    )
    pool = FakePool()
    store = PostgresOrderStore(pool)  # type: ignore[arg-type]

    async with store._request_transaction():
        pass

    assert pool.acquire_timeouts == [None]
    assert pool.connection.executions == []


@pytest.mark.asyncio
async def test_database_operation_fails_before_pool_wait_when_reserve_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        postgres_module,
        "remaining_request_seconds",
        lambda *, reserve_ms: 0.0,
    )
    pool = FakePool()
    store = PostgresOrderStore(pool)  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="exhausted before database"):
        async with store._request_transaction():
            pass

    assert pool.acquire_timeouts == []
