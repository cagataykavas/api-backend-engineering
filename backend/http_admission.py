from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyAdmissionMiddleware:
    """Buffer and validate a bounded HTTP body before application side effects."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_receive_messages: int = 1024,
    ) -> None:
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes <= 0
        ):
            raise ValueError("max_body_bytes must be a positive integer")
        if (
            isinstance(max_receive_messages, bool)
            or not isinstance(max_receive_messages, int)
            or max_receive_messages <= 0
        ):
            raise ValueError("max_receive_messages must be a positive integer")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_receive_messages = max_receive_messages

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        reason, declared_length = self._validate_framing(scope.get("headers", []))
        if reason is not None:
            await self._reject(send, reason, status=400)
            return
        if declared_length is not None and declared_length > self.max_body_bytes:
            await self._reject(send, "payload_too_large", status=413)
            return

        body = bytearray()
        for _ in range(self.max_receive_messages):
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return
            if message_type != "http.request":
                await self._reject(send, "invalid_asgi_message", status=400)
                return

            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                await self._reject(send, "invalid_body_chunk", status=400)
                return
            body.extend(chunk)
            if len(body) > self.max_body_bytes:
                await self._reject(send, "payload_too_large", status=413)
                return
            if not message.get("more_body", False):
                break
        else:
            await self._reject(send, "too_many_body_chunks", status=400)
            return

        if declared_length is not None and len(body) != declared_length:
            await self._reject(send, "content_length_mismatch", status=400)
            return

        replayed = False

        async def replay_body() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_body, send)

    def _validate_framing(
        self,
        raw_headers: list[tuple[bytes, bytes]],
    ) -> tuple[str | None, int | None]:
        content_lengths = [
            value for name, value in raw_headers if name.lower() == b"content-length"
        ]
        has_transfer_encoding = any(name.lower() == b"transfer-encoding" for name, _ in raw_headers)

        if len(content_lengths) > 1:
            return "duplicate_content_length", None
        if content_lengths and has_transfer_encoding:
            return "ambiguous_message_framing", None
        if not content_lengths:
            return None, None

        raw_length = content_lengths[0]
        try:
            text = raw_length.decode("ascii")
        except UnicodeDecodeError:
            return "invalid_content_length", None
        if not text or not text.isdecimal() or text != text.strip():
            return "invalid_content_length", None

        try:
            declared_length = int(text)
        except ValueError:
            return "invalid_content_length", None
        return None, declared_length

    @staticmethod
    async def _reject(send: Send, reason: str, *, status: int) -> None:
        body = json.dumps(
            {"detail": "request rejected", "reason": reason},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"x-request-admission-reason", reason.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
