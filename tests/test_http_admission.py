from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import create_app
from backend.http_admission import RequestBodyAdmissionMiddleware


def _scope(headers: Iterable[tuple[bytes, bytes]] = ()) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


async def _exercise(
    *,
    headers: Iterable[tuple[bytes, bytes]] = (),
    messages: list[dict[str, Any]],
    max_body_bytes: int = 8,
    max_receive_messages: int = 1024,
) -> tuple[list[dict[str, Any]], list[bytes]]:
    sent: list[dict[str, Any]] = []
    received_by_app: list[bytes] = []
    pending = iter(messages)

    async def receive() -> dict[str, Any]:
        return next(pending)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def downstream(scope, replay_receive, downstream_send) -> None:
        message = await replay_receive()
        received_by_app.append(message["body"])
        await downstream_send({"type": "http.response.start", "status": 204, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b""})

    middleware = RequestBodyAdmissionMiddleware(
        downstream,
        max_body_bytes=max_body_bytes,
        max_receive_messages=max_receive_messages,
    )
    await middleware(_scope(headers), receive, send)
    return sent, received_by_app


def _reason(sent: list[dict[str, Any]]) -> str:
    return json.loads(sent[1]["body"])["reason"]


@pytest.mark.asyncio
async def test_valid_chunks_are_replayed_once_after_full_admission() -> None:
    sent, received = await _exercise(
        headers=[(b"content-length", b"4")],
        messages=[
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
        ],
    )

    assert sent[0]["status"] == 204
    assert received == [b"abcd"]


@pytest.mark.asyncio
async def test_declared_oversize_body_is_rejected_without_reading() -> None:
    sent, received = await _exercise(
        headers=[(b"content-length", b"9")],
        messages=[],
    )

    assert sent[0]["status"] == 413
    assert _reason(sent) == "payload_too_large"
    assert received == []


@pytest.mark.asyncio
async def test_streamed_oversize_body_is_rejected_before_downstream() -> None:
    sent, received = await _exercise(
        messages=[
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ],
    )

    assert sent[0]["status"] == 413
    assert _reason(sent) == "payload_too_large"
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ([(b"content-length", b"1"), (b"content-length", b"1")], "duplicate_content_length"),
        (
            [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
            "ambiguous_message_framing",
        ),
        ([(b"content-length", b" 1")], "invalid_content_length"),
        ([(b"content-length", b"-1")], "invalid_content_length"),
        ([(b"content-length", b"9" * 5000)], "invalid_content_length"),
    ],
)
async def test_ambiguous_or_invalid_framing_fails_closed(headers, reason) -> None:
    sent, received = await _exercise(headers=headers, messages=[])

    assert sent[0]["status"] == 400
    assert _reason(sent) == reason
    assert received == []


@pytest.mark.asyncio
async def test_content_length_mismatch_fails_closed() -> None:
    sent, received = await _exercise(
        headers=[(b"content-length", b"4")],
        messages=[{"type": "http.request", "body": b"abc", "more_body": False}],
    )

    assert sent[0]["status"] == 400
    assert _reason(sent) == "content_length_mismatch"
    assert received == []


@pytest.mark.asyncio
async def test_chunk_count_is_bounded_even_for_zero_length_chunks() -> None:
    sent, received = await _exercise(
        messages=[
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
        ],
        max_receive_messages=2,
    )

    assert sent[0]["status"] == 400
    assert _reason(sent) == "too_many_body_chunks"
    assert received == []


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_body_limit_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        RequestBodyAdmissionMiddleware(lambda *_: None, max_body_bytes=value)


def test_fastapi_integration_rejects_before_route_side_effects() -> None:
    app = create_app(max_request_body_bytes=32)
    with TestClient(app) as client:
        response = client.post(
            "/v1/items",
            content=b"x" * 33,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["reason"] == "payload_too_large"
    assert response.headers["x-request-admission-reason"] == "payload_too_large"
