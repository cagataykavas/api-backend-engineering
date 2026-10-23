"""Deadline-aware outbound HTTP adapter with fixed destination authority."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from backend.request_deadline import remaining_request_seconds

TIMEOUT_HEADER = "X-Request-Timeout-Ms"
_ALLOWED_METHODS = frozenset({"DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"})
_FORBIDDEN_CALLER_HEADERS = frozenset(
    {"content-length", "host", "transfer-encoding", TIMEOUT_HEADER.casefold()}
)


class OutboundRequestError(RuntimeError):
    """A stable, non-sensitive outbound request failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OutboundDeadlineExceeded(OutboundRequestError):
    """The request budget expired before the dependency completed."""


@dataclass(frozen=True)
class OutboundHttpPolicy:
    default_timeout_ms: int = 2_000
    max_timeout_ms: int = 5_000
    response_reserve_ms: int = 25
    downstream_reserve_ms: int = 10
    min_dispatch_ms: int = 20
    connect_timeout_ms: int = 500
    pool_timeout_ms: int = 100
    max_path_chars: int = 2_048

    def __post_init__(self) -> None:
        values = (
            self.default_timeout_ms,
            self.max_timeout_ms,
            self.response_reserve_ms,
            self.downstream_reserve_ms,
            self.min_dispatch_ms,
            self.connect_timeout_ms,
            self.pool_timeout_ms,
            self.max_path_chars,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("outbound HTTP policy values must be non-negative integers")
        if self.default_timeout_ms <= 0 or self.max_timeout_ms <= 0:
            raise ValueError("outbound HTTP timeouts must be positive")
        if self.default_timeout_ms > self.max_timeout_ms:
            raise ValueError("default_timeout_ms cannot exceed max_timeout_ms")
        if self.min_dispatch_ms <= self.downstream_reserve_ms:
            raise ValueError("min_dispatch_ms must exceed downstream_reserve_ms")
        if self.min_dispatch_ms > self.max_timeout_ms:
            raise ValueError("min_dispatch_ms cannot exceed max_timeout_ms")
        if self.min_dispatch_ms > self.default_timeout_ms:
            raise ValueError("min_dispatch_ms cannot exceed default_timeout_ms")
        if self.connect_timeout_ms <= 0 or self.pool_timeout_ms <= 0:
            raise ValueError("phase timeouts must be positive")
        if self.max_path_chars < 1 or self.max_path_chars > 16_384:
            raise ValueError("max_path_chars must be between 1 and 16384")


class DeadlineHttpClient:
    """Propagate a shrinking request budget through one pooled HTTP client."""

    def __init__(
        self,
        base_url: str,
        *,
        policy: OutboundHttpPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an absolute HTTP(S) authority without credentials")
        self.policy = policy or OutboundHttpPolicy()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            timeout=None,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _budgets_ms(self) -> tuple[int, int]:
        remaining = remaining_request_seconds(reserve_ms=self.policy.response_reserve_ms)
        if remaining is None:
            available_ms = self.policy.default_timeout_ms
        elif not math.isfinite(remaining) or remaining < 0:
            raise OutboundRequestError("INVALID_REQUEST_DEADLINE")
        else:
            available_ms = math.floor(remaining * 1_000)

        dispatch_ms = min(available_ms, self.policy.max_timeout_ms)
        if dispatch_ms < self.policy.min_dispatch_ms:
            raise OutboundDeadlineExceeded("OUTBOUND_DEADLINE_EXHAUSTED")
        return dispatch_ms, dispatch_ms - self.policy.downstream_reserve_ms

    def _request_headers(
        self,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None,
        downstream_ms: int,
    ) -> httpx.Headers:
        resolved = httpx.Headers(headers)
        if any(name.casefold() in _FORBIDDEN_CALLER_HEADERS for name, _ in resolved.multi_items()):
            raise OutboundRequestError("FORBIDDEN_OUTBOUND_HEADER")
        resolved[TIMEOUT_HEADER] = str(downstream_ms)
        return resolved

    def _validate_target(self, method: str, path: str) -> str:
        normalized_method = method.upper()
        if method != normalized_method or normalized_method not in _ALLOWED_METHODS:
            raise OutboundRequestError("INVALID_OUTBOUND_METHOD")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > self.policy.max_path_chars
            or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise OutboundRequestError("INVALID_OUTBOUND_PATH")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise OutboundRequestError("INVALID_OUTBOUND_PATH")
        return normalized_method

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Execute one bounded request without allowing authority or budget overrides."""

        normalized_method = self._validate_target(method, path)
        if content is not None and json is not None:
            raise OutboundRequestError("AMBIGUOUS_OUTBOUND_BODY")
        dispatch_ms, downstream_ms = self._budgets_ms()
        request_headers = self._request_headers(headers, downstream_ms)
        dispatch_seconds = dispatch_ms / 1_000.0
        timeout = httpx.Timeout(
            timeout=dispatch_seconds,
            connect=min(dispatch_seconds, self.policy.connect_timeout_ms / 1_000.0),
            pool=min(dispatch_seconds, self.policy.pool_timeout_ms / 1_000.0),
        )
        request = self._client.build_request(
            normalized_method,
            path,
            headers=request_headers,
            content=content,
            json=json,
            timeout=timeout,
        )
        try:
            async with asyncio.timeout(dispatch_seconds):
                return await self._client.send(request)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise OutboundDeadlineExceeded("OUTBOUND_DEADLINE_EXCEEDED") from exc
