from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app import create_app, positive_int_env
from backend.request_deadline import (
    RequestDeadlineMiddleware,
    get_request_deadline,
    remaining_request_seconds,
)

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }


async def invoke(
    app: ASGIApp,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[Message]:
    received = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        sent.append(message)

    await app(http_scope(headers), receive, send)
    return sent


def response_json(messages: list[Message]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return json.loads(body)


def response_headers(messages: list[Message]) -> dict[bytes, bytes]:
    start = next(message for message in messages if message["type"] == "http.response.start")
    return dict(start["headers"])


def simple_app(observed: dict[str, Any]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        deadline = get_request_deadline()
        observed["deadline"] = deadline
        observed["remaining"] = remaining_request_seconds(reserve_ms=1)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    return app


@pytest.mark.asyncio
async def test_default_deadline_is_propagated_and_echoed() -> None:
    observed: dict[str, Any] = {}
    middleware = RequestDeadlineMiddleware(simple_app(observed), default_timeout_ms=100)

    messages = await invoke(middleware)

    assert messages[0]["status"] == 200
    assert response_headers(messages)[b"x-request-timeout-ms"] == b"100"
    assert observed["deadline"].timeout_ms == 100
    assert observed["deadline"].source == "server_default"
    assert 0 < observed["remaining"] <= 0.099
    assert get_request_deadline() is None


@pytest.mark.asyncio
async def test_client_timeout_is_admitted_within_policy() -> None:
    observed: dict[str, Any] = {}
    middleware = RequestDeadlineMiddleware(
        simple_app(observed),
        default_timeout_ms=100,
        max_timeout_ms=500,
    )

    messages = await invoke(middleware, headers=[(b"x-request-timeout-ms", b"250")])

    assert messages[0]["status"] == 200
    assert observed["deadline"].timeout_ms == 250
    assert observed["deadline"].source == "client"
    assert response_headers(messages)[b"x-request-timeout-ms"] == b"250"


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-request-timeout-ms", b"0")],
        [(b"x-request-timeout-ms", b" 10")],
        [(b"x-request-timeout-ms", b"+10")],
        [(b"x-request-timeout-ms", b"10.5")],
        [(b"x-request-timeout-ms", b"501")],
        [(b"x-request-timeout-ms", b"10"), (b"X-Request-Timeout-Ms", b"10")],
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_ambiguous_timeout_fails_closed(
    headers: list[tuple[bytes, bytes]],
) -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    middleware = RequestDeadlineMiddleware(app, default_timeout_ms=100, max_timeout_ms=500)
    messages = await invoke(middleware, headers=headers)

    assert messages[0]["status"] == 400
    assert response_json(messages)["code"] == "invalid_request_timeout"
    assert response_headers(messages)[b"content-type"] == b"application/problem+json"
    assert called is False


@pytest.mark.asyncio
async def test_timeout_cancels_inflight_application_before_response() -> None:
    cancelled = asyncio.Event()

    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    middleware = RequestDeadlineMiddleware(
        slow_app,
        default_timeout_ms=10,
        max_timeout_ms=100,
    )

    messages = await invoke(middleware)

    assert cancelled.is_set()
    assert messages[0]["status"] == 504
    assert response_json(messages)["code"] == "request_deadline_exceeded"
    assert response_headers(messages)[b"x-request-timeout-ms"] == b"10"
    assert get_request_deadline() is None


@pytest.mark.asyncio
async def test_timeout_after_headers_closes_stream_instead_of_double_response() -> None:
    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(60)

    middleware = RequestDeadlineMiddleware(
        streaming_app,
        default_timeout_ms=10,
        max_timeout_ms=100,
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    with pytest.raises(TimeoutError):
        await middleware(http_scope(), receive, send)

    assert [message["type"] for message in sent] == ["http.response.start"]


@pytest.mark.asyncio
async def test_timeout_after_complete_response_only_cancels_post_response_work() -> None:
    cancelled = asyncio.Event()

    async def app_with_post_response_work(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    middleware = RequestDeadlineMiddleware(
        app_with_post_response_work,
        default_timeout_ms=10,
        max_timeout_ms=100,
    )

    messages = await invoke(middleware)

    assert cancelled.is_set()
    assert [message["type"] for message in messages] == [
        "http.response.start",
        "http.response.body",
    ]
    assert messages[0]["status"] == 200


def test_fastapi_health_exposes_applied_budget() -> None:
    application = create_app(request_timeout_ms=250, max_request_timeout_ms=1_000)

    with TestClient(application) as client:
        response = client.get("/health", headers={"X-Request-Timeout-Ms": "400"})

    assert response.status_code == 200
    assert response.headers["X-Request-Timeout-Ms"] == "400"


def test_fastapi_returns_problem_response_when_route_exceeds_budget() -> None:
    application = create_app(request_timeout_ms=100, max_request_timeout_ms=1_000)

    @application.get("/slow-test-route")
    async def slow_test_route() -> dict[str, bool]:
        await asyncio.sleep(60)
        return {"completed": True}

    with TestClient(application) as client:
        response = client.get(
            "/slow-test-route",
            headers={"X-Request-Timeout-Ms": "10"},
        )

    assert response.status_code == 504
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "request_deadline_exceeded"
    assert response.headers["X-Request-Timeout-Ms"] == "10"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_timeout_ms": 0},
        {"default_timeout_ms": True},
        {"default_timeout_ms": 100, "min_timeout_ms": 200},
        {"default_timeout_ms": 200, "max_timeout_ms": 100},
    ],
)
def test_invalid_server_policy_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="deadline limits"):
        RequestDeadlineMiddleware(simple_app({}), **kwargs)


def test_remaining_budget_validation() -> None:
    assert remaining_request_seconds() is None
    with pytest.raises(ValueError, match="reserve_ms"):
        remaining_request_seconds(reserve_ms=-1)


@pytest.mark.parametrize("value", ["0", "01", "+10", " 10", "10.5", "ten"])
def test_environment_timeout_must_be_canonical_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_MS", value)

    with pytest.raises(ValueError, match="canonical positive integer"):
        positive_int_env("REQUEST_TIMEOUT_MS", 2_000)


def test_environment_timeout_uses_default_or_valid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUEST_TIMEOUT_MS", raising=False)
    assert positive_int_env("REQUEST_TIMEOUT_MS", 2_000) == 2_000

    monkeypatch.setenv("REQUEST_TIMEOUT_MS", "750")
    assert positive_int_env("REQUEST_TIMEOUT_MS", 2_000) == 750
