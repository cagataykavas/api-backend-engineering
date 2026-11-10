from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.trace_context import (
    MAX_TRACE_HEADER_BYTES,
    TraceContextAdmissionMiddleware,
    TraceContextError,
    admit_trace_context,
)

VALID_TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def header(name: bytes, value: bytes) -> tuple[bytes, bytes]:
    return name, value


def test_accepts_canonical_trace_context_and_preserves_order() -> None:
    headers = [
        header(b"host", b"example.test"),
        header(b"traceparent", VALID_TRACEPARENT),
        header(b"tracestate", b"vendor=value,tenant@system=opaque"),
    ]

    decision = admit_trace_context(headers)

    assert decision.headers == tuple(headers)
    assert decision.baggage_dropped is False


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7", "invalid_traceparent"),
        (b"ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", "invalid_traceparent"),
        (b"00-00000000000000000000000000000000-00f067aa0ba902b7-01", "zero_trace_identifier"),
        (b"00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01", "zero_trace_identifier"),
        (b"00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01", "invalid_traceparent"),
    ],
)
def test_rejects_noncanonical_traceparents(value: bytes, reason: str) -> None:
    with pytest.raises(TraceContextError, match=reason):
        admit_trace_context([header(b"traceparent", value)])


def test_rejects_duplicate_traceparent_and_orphan_tracestate() -> None:
    with pytest.raises(TraceContextError, match="duplicate_traceparent"):
        admit_trace_context(
            [header(b"traceparent", VALID_TRACEPARENT), header(b"TraceParent", VALID_TRACEPARENT)]
        )
    with pytest.raises(TraceContextError, match="orphan_tracestate"):
        admit_trace_context([header(b"tracestate", b"vendor=value")])


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"vendor=one,vendor=two", "duplicate_tracestate_key"),
        (b"Vendor=value", "invalid_tracestate"),
        (b"vendor=has=separator", "invalid_tracestate"),
        (b"vendor= value", "invalid_tracestate"),
        (b"vendor=value,", "invalid_tracestate"),
    ],
)
def test_rejects_ambiguous_tracestate(value: bytes, reason: str) -> None:
    with pytest.raises(TraceContextError, match=reason):
        admit_trace_context(
            [header(b"traceparent", VALID_TRACEPARENT), header(b"tracestate", value)]
        )


def test_enforces_aggregate_trace_header_budget() -> None:
    oversized = b"x" * MAX_TRACE_HEADER_BYTES
    with pytest.raises(TraceContextError, match="trace_headers_too_large"):
        admit_trace_context([header(b"baggage", oversized)])


def test_drops_baggage_without_exposing_its_value() -> None:
    headers = [
        header(b"traceparent", VALID_TRACEPARENT),
        header(b"baggage", b"customer_email=private@example.test"),
        header(b"x-request-id", b"request-1"),
    ]

    decision = admit_trace_context(headers)

    assert decision.baggage_dropped is True
    assert decision.headers == (
        header(b"traceparent", VALID_TRACEPARENT),
        header(b"x-request-id", b"request-1"),
    )
    assert b"private@example.test" not in repr(decision).encode()


def test_middleware_rejects_before_handler_and_strips_baggage() -> None:
    application = FastAPI()
    application.add_middleware(TraceContextAdmissionMiddleware)

    @application.get("/")
    def inspect_headers(request: Request) -> dict[str, bool]:
        return {"has_baggage": "baggage" in request.headers}

    with TestClient(application) as client:
        accepted = client.get(
            "/",
            headers={
                "traceparent": VALID_TRACEPARENT.decode(),
                "baggage": "customer_email=private@example.test",
            },
        )
        rejected = client.get("/", headers={"traceparent": "not-valid"})

    assert accepted.status_code == 200
    assert accepted.json() == {"has_baggage": False}
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "invalid trace context"}
