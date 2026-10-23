"""ASGI request-deadline admission and propagation.

The middleware enforces one wall-clock budget around the complete application
call. Because the timeout cancels the in-flight coroutine, cancellation reaches
awaited database, cache, and HTTP-client operations instead of merely replacing
their eventual response.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_TIMEOUT_HEADER = b"x-request-timeout-ms"
_TIMEOUT_PATTERN = re.compile(rb"[1-9][0-9]{0,8}")


@dataclass(frozen=True)
class RequestDeadline:
    """Monotonic deadline visible to downstream adapters in this request task."""

    timeout_ms: int
    started_at: float
    deadline_at: float
    source: str

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())


_CURRENT_DEADLINE: ContextVar[RequestDeadline | None] = ContextVar(
    "request_deadline",
    default=None,
)


class InvalidRequestTimeout(ValueError):
    """The client supplied an ambiguous or out-of-policy timeout header."""


def get_request_deadline() -> RequestDeadline | None:
    """Return the current request deadline, or ``None`` outside a request."""

    return _CURRENT_DEADLINE.get()


def remaining_request_seconds(*, reserve_ms: int = 0) -> float | None:
    """Return a safe timeout value for a downstream operation.

    ``reserve_ms`` keeps a small response-serialization budget at the caller.
    The value is monotonic and never negative.
    """

    if type(reserve_ms) is not int or reserve_ms < 0:
        raise ValueError("reserve_ms must be a non-negative integer")
    deadline = get_request_deadline()
    if deadline is None:
        return None
    return max(0.0, deadline.remaining_seconds() - reserve_ms / 1000.0)


class RequestDeadlineMiddleware:
    """Enforce a bounded client-requested or server-default request deadline."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_timeout_ms: int = 2_000,
        max_timeout_ms: int = 10_000,
        min_timeout_ms: int = 1,
    ) -> None:
        limits = (default_timeout_ms, max_timeout_ms, min_timeout_ms)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("deadline limits must be positive integers")
        if min_timeout_ms > default_timeout_ms or default_timeout_ms > max_timeout_ms:
            raise ValueError("deadline limits must satisfy min <= default <= max")
        self.app = app
        self.default_timeout_ms = default_timeout_ms
        self.max_timeout_ms = max_timeout_ms
        self.min_timeout_ms = min_timeout_ms

    def _resolve_timeout(self, scope: Scope) -> tuple[int, str]:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == REQUEST_TIMEOUT_HEADER
        ]
        if not values:
            return self.default_timeout_ms, "server_default"
        if len(values) != 1:
            raise InvalidRequestTimeout("request timeout header must appear exactly once")
        raw = values[0]
        if not _TIMEOUT_PATTERN.fullmatch(raw):
            raise InvalidRequestTimeout("request timeout must be canonical positive milliseconds")
        timeout_ms = int(raw)
        if timeout_ms < self.min_timeout_ms or timeout_ms > self.max_timeout_ms:
            raise InvalidRequestTimeout(
                f"request timeout must be between {self.min_timeout_ms} and "
                f"{self.max_timeout_ms} milliseconds"
            )
        return timeout_ms, "client"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            timeout_ms, source = self._resolve_timeout(scope)
        except InvalidRequestTimeout as exc:
            await _send_problem(
                send,
                status=400,
                code="invalid_request_timeout",
                title="Invalid request timeout",
                detail=str(exc),
            )
            return

        started_at = time.monotonic()
        deadline = RequestDeadline(
            timeout_ms=timeout_ms,
            started_at=started_at,
            deadline_at=started_at + timeout_ms / 1000.0,
            source=source,
        )
        token: Token[RequestDeadline | None] = _CURRENT_DEADLINE.set(deadline)
        response_started = False
        response_complete = False

        async def send_with_deadline(message: Message) -> None:
            nonlocal response_complete, response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != REQUEST_TIMEOUT_HEADER
                ]
                headers.append((REQUEST_TIMEOUT_HEADER, str(timeout_ms).encode("ascii")))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        try:
            try:
                async with asyncio.timeout(deadline.remaining_seconds()):
                    await self.app(scope, receive, send_with_deadline)
            except TimeoutError:
                if response_complete:
                    # The client already has a complete response. Cancellation has
                    # stopped post-response work; do not turn that into a server error.
                    return
                if response_started:
                    # Status and headers are already on the wire. Raising closes the
                    # incomplete stream rather than emitting an invalid second response.
                    raise
                await _send_problem(
                    send,
                    status=504,
                    code="request_deadline_exceeded",
                    title="Request deadline exceeded",
                    detail="The request exceeded its admitted processing budget.",
                    extra_headers=[(REQUEST_TIMEOUT_HEADER, str(timeout_ms).encode("ascii"))],
                )
        finally:
            _CURRENT_DEADLINE.reset(token)


async def _send_problem(
    send: Send,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(
        {
            "type": f"urn:api-backend-engineering:error:{code}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/problem+json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload, "more_body": False})
