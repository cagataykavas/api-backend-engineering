from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.types import Message, Scope

from backend.http_client import (
    DeadlineHttpClient,
    OutboundDeadlineExceeded,
    OutboundHttpPolicy,
    OutboundRequestError,
)
from backend.request_deadline import RequestDeadlineMiddleware


def policy(**overrides: int) -> OutboundHttpPolicy:
    values = {
        "default_timeout_ms": 200,
        "max_timeout_ms": 500,
        "response_reserve_ms": 25,
        "downstream_reserve_ms": 10,
        "min_dispatch_ms": 20,
        "connect_timeout_ms": 100,
        "pool_timeout_ms": 50,
        **overrides,
    }
    return OutboundHttpPolicy(**values)


@pytest.mark.asyncio
async def test_remaining_budget_bounds_transport_and_propagated_header(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def remaining(*, reserve_ms: int) -> float:
        observed["reserve_ms"] = reserve_ms
        return 0.1499

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["request"] = request
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("backend.http_client.remaining_request_seconds", remaining)
    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.request("GET", "/v1/items?limit=1")

    request = observed["request"]
    assert isinstance(request, httpx.Request)
    assert response.json() == {"ok": True}
    assert observed["reserve_ms"] == 25
    assert request.headers["X-Request-Timeout-Ms"] == "139"
    assert request.extensions["timeout"] == {
        "connect": 0.1,
        "read": 0.149,
        "write": 0.149,
        "pool": 0.05,
    }


@pytest.mark.asyncio
async def test_middleware_deadline_reaches_real_outbound_adapter() -> None:
    observed_timeout_ms = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_timeout_ms
        observed_timeout_ms = int(request.headers["X-Request-Timeout-Ms"])
        return httpx.Response(200, content=b"dependency-ok")

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/proxy",
        "raw_path": b"/proxy",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:

        async def app(_scope: Scope, _receive, app_send) -> None:
            response = await client.request("GET", "/health")
            await app_send(
                {"type": "http.response.start", "status": response.status_code, "headers": []}
            )
            await app_send({"type": "http.response.body", "body": response.content})

        await RequestDeadlineMiddleware(app, default_timeout_ms=200, max_timeout_ms=500)(
            scope, receive, send
        )

    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"dependency-ok"
    assert 0 < observed_timeout_ms < 200


@pytest.mark.asyncio
async def test_request_external_work_uses_default_bounded_budget(monkeypatch) -> None:
    observed: list[httpx.Request] = []
    monkeypatch.setattr("backend.http_client.remaining_request_seconds", lambda **_kwargs: None)

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(204)

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(default_timeout_ms=200),
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.request("POST", "/refresh", json={"scope": "recent"})

    assert response.status_code == 204
    assert observed[0].headers["X-Request-Timeout-Ms"] == "190"
    assert observed[0].extensions["timeout"]["read"] == 0.2


@pytest.mark.asyncio
async def test_exhausted_budget_fails_before_transport(monkeypatch) -> None:
    calls = 0
    monkeypatch.setattr("backend.http_client.remaining_request_seconds", lambda **_kwargs: 0.0199)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OutboundDeadlineExceeded, match="OUTBOUND_DEADLINE_EXHAUSTED"):
            await client.request("GET", "/v1/items")

    assert calls == 0


@pytest.mark.asyncio
async def test_outer_monotonic_timeout_cancels_slow_transport(monkeypatch) -> None:
    cancelled = asyncio.Event()
    monkeypatch.setattr("backend.http_client.remaining_request_seconds", lambda **_kwargs: 0.025)

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()
        return httpx.Response(200)

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(min_dispatch_ms=20, downstream_reserve_ms=5),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OutboundDeadlineExceeded, match="OUTBOUND_DEADLINE_EXCEEDED"):
            await client.request("GET", "/slow")

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_httpx_phase_timeout_is_normalized(monkeypatch) -> None:
    monkeypatch.setattr("backend.http_client.remaining_request_seconds", lambda **_kwargs: 0.1)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive upstream detail", request=request)

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OutboundDeadlineExceeded) as caught:
            await client.request("GET", "/slow")

    assert caught.value.code == "OUTBOUND_DEADLINE_EXCEEDED"
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "inventory.internal",
        "ftp://inventory.internal",
        "https://user:secret@inventory.internal",
        "https://inventory.internal/api",
        "https://inventory.internal?target=other",
        "https://inventory.internal#fragment",
    ],
)
def test_base_url_must_pin_a_safe_authority(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        DeadlineHttpClient(base_url)


@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("get", "/items", "INVALID_OUTBOUND_METHOD"),
        ("TRACE", "/items", "INVALID_OUTBOUND_METHOD"),
        ("GET", "items", "INVALID_OUTBOUND_PATH"),
        ("GET", "//attacker.example/items", "INVALID_OUTBOUND_PATH"),
        ("GET", "/\\attacker.example/items", "INVALID_OUTBOUND_PATH"),
        ("GET", "/items\nX-Injected: true", "INVALID_OUTBOUND_PATH"),
        ("GET", "/items#secret", "INVALID_OUTBOUND_PATH"),
    ],
)
@pytest.mark.asyncio
async def test_method_and_target_are_validated_without_dispatch(
    method: str, path: str, code: str
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OutboundRequestError, match=code):
            await client.request(method, path)

    assert calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Request-Timeout-Ms": "9999"},
        {"Host": "attacker.example"},
        {"Content-Length": "0"},
        {"Transfer-Encoding": "chunked"},
    ],
)
@pytest.mark.asyncio
async def test_caller_cannot_override_framing_authority_or_budget(headers: dict[str, str]) -> None:
    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    ) as client:
        with pytest.raises(OutboundRequestError, match="FORBIDDEN_OUTBOUND_HEADER"):
            await client.request("GET", "/items", headers=headers)


@pytest.mark.asyncio
async def test_redirects_are_returned_without_following_new_authority() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.example/collect"})

    async with DeadlineHttpClient(
        "https://inventory.internal",
        policy=policy(),
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.request("GET", "/redirect")

    assert response.status_code == 302
    assert paths == ["https://inventory.internal/redirect"]


def test_policy_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="default_timeout_ms"):
        policy(default_timeout_ms=501, max_timeout_ms=500)
    with pytest.raises(ValueError, match="min_dispatch_ms"):
        policy(min_dispatch_ms=10, downstream_reserve_ms=10)
    with pytest.raises(ValueError, match="default_timeout_ms"):
        policy(default_timeout_ms=20, min_dispatch_ms=21)
    with pytest.raises(ValueError, match="phase timeouts"):
        policy(connect_timeout_ms=0)
    with pytest.raises(ValueError, match="non-negative integers"):
        policy(response_reserve_ms=True)
