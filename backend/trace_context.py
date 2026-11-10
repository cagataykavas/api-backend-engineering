from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from prometheus_client import Counter
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_TRACE_HEADER_BYTES: Final = 2_048
MAX_TRACE_HEADER_LINES: Final = 8
MAX_TRACESTATE_BYTES: Final = 512
MAX_TRACESTATE_MEMBERS: Final = 32

_TRACEPARENT = re.compile(rb"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_SIMPLE_KEY = re.compile(r"^[a-z][a-z0-9_\-*/]{0,255}$")
_TENANT_KEY = re.compile(r"^[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13}$")

TRACE_CONTEXT_REJECTIONS = Counter(
    "api_trace_context_rejections_total",
    "Inbound trace contexts rejected before application handling",
    ["reason"],
)
TRACE_BAGGAGE_DROPPED = Counter(
    "api_trace_baggage_dropped_total",
    "Requests whose untrusted W3C baggage was removed",
)


class TraceContextError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class TraceContextDecision:
    headers: tuple[tuple[bytes, bytes], ...]
    baggage_dropped: bool


def _validate_traceparent(values: list[bytes]) -> None:
    if len(values) > 1:
        raise TraceContextError("duplicate_traceparent")
    if not values:
        return

    match = _TRACEPARENT.fullmatch(values[0])
    if match is None:
        raise TraceContextError("invalid_traceparent")
    trace_id, parent_id, _flags = match.groups()
    if trace_id == b"0" * 32 or parent_id == b"0" * 16:
        raise TraceContextError("zero_trace_identifier")


def _valid_tracestate_key(key: str) -> bool:
    return _SIMPLE_KEY.fullmatch(key) is not None or _TENANT_KEY.fullmatch(key) is not None


def _validate_tracestate(values: list[bytes], *, traceparent_present: bool) -> None:
    if not values:
        return
    if not traceparent_present:
        raise TraceContextError("orphan_tracestate")

    combined = b",".join(values)
    if len(combined) > MAX_TRACESTATE_BYTES:
        raise TraceContextError("tracestate_too_large")
    try:
        text = combined.decode("ascii")
    except UnicodeDecodeError as error:
        raise TraceContextError("invalid_tracestate") from error

    members = [member.strip(" \t") for member in text.split(",")]
    if (
        not members
        or len(members) > MAX_TRACESTATE_MEMBERS
        or any(not member for member in members)
    ):
        raise TraceContextError("invalid_tracestate")

    seen: set[str] = set()
    for member in members:
        if member.count("=") != 1:
            raise TraceContextError("invalid_tracestate")
        key, value = member.split("=", 1)
        if not _valid_tracestate_key(key):
            raise TraceContextError("invalid_tracestate")
        if key in seen:
            raise TraceContextError("duplicate_tracestate_key")
        seen.add(key)
        if not value or len(value) > 256 or value != value.strip(" \t"):
            raise TraceContextError("invalid_tracestate")
        if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
            raise TraceContextError("invalid_tracestate")
        if "," in value or "=" in value:
            raise TraceContextError("invalid_tracestate")


def admit_trace_context(headers: list[tuple[bytes, bytes]]) -> TraceContextDecision:
    """Validate trace correlation headers and remove untrusted baggage.

    Only W3C trace-context version 00 is accepted. Baggage is deliberately not
    propagated because it may contain user-controlled or sensitive metadata.
    """
    trace_headers = [
        (name.lower(), value)
        for name, value in headers
        if name.lower() in {b"traceparent", b"tracestate", b"baggage"}
    ]
    if len(trace_headers) > MAX_TRACE_HEADER_LINES:
        raise TraceContextError("too_many_trace_headers")
    if sum(len(name) + len(value) for name, value in trace_headers) > MAX_TRACE_HEADER_BYTES:
        raise TraceContextError("trace_headers_too_large")

    traceparent = [value for name, value in trace_headers if name == b"traceparent"]
    tracestate = [value for name, value in trace_headers if name == b"tracestate"]
    _validate_traceparent(traceparent)
    _validate_tracestate(tracestate, traceparent_present=bool(traceparent))

    baggage_dropped = any(name == b"baggage" for name, _value in trace_headers)
    sanitized = tuple((name, value) for name, value in headers if name.lower() != b"baggage")
    return TraceContextDecision(sanitized, baggage_dropped)


class TraceContextAdmissionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            decision = admit_trace_context(list(scope.get("headers", [])))
        except TraceContextError as error:
            TRACE_CONTEXT_REJECTIONS.labels(error.reason).inc()
            response = JSONResponse({"detail": "invalid trace context"}, status_code=400)
            await response(scope, receive, send)
            return

        if decision.baggage_dropped:
            TRACE_BAGGAGE_DROPPED.inc()
        scope["headers"] = list(decision.headers)
        await self.app(scope, receive, send)
