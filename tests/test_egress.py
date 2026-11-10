from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from backend.egress import (
    EgressConnectionError,
    UnsafeEgressTarget,
    open_verified_tls_stream,
    resolve_public_https_target,
)


def record(address: str) -> tuple[int, int, int, str, tuple[Any, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)


def resolver_for(*addresses: str):
    async def resolve(host: str, port: int):
        assert host == "example.com"
        assert port == 443
        return [record(address) for address in addresses]

    return resolve


class FakeWriter:
    def __init__(self, peer_address: str) -> None:
        self.peer_address = peer_address
        self.closed = False

    def get_extra_info(self, name: str):
        assert name == "peername"
        return (self.peer_address, 443)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_resolve_normalizes_host_and_preserves_request_target() -> None:
    target = await resolve_public_https_target(
        "https://EXAMPLE.com./hooks/order?attempt=2",
        resolver=resolver_for("93.184.216.34", "2606:4700:4700::1111"),
    )

    assert target.host == "example.com"
    assert target.port == 443
    assert target.request_target == "/hooks/order?attempt=2"
    assert tuple(str(address) for address in target.addresses) == (
        "93.184.216.34",
        "2606:4700:4700::1111",
    )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.com/hook", "only https"),
        ("https://user:secret@example.com/hook", "credentials"),
        ("https://example.com:8443/hook", "port 8443"),
        ("https://example.com/hook#fragment", "fragments"),
        ("https:///missing-host", "include a host"),
    ],
)
async def test_resolve_rejects_unsafe_url_shapes(url: str, message: str) -> None:
    with pytest.raises(UnsafeEgressTarget, match=message):
        await resolve_public_https_target(url, resolver=resolver_for("93.184.216.34"))


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.7",),
        ("::1",),
        ("93.184.216.34", "192.168.1.5"),
    ],
)
async def test_resolve_rejects_private_or_mixed_dns_answers(addresses: tuple[str, ...]) -> None:
    with pytest.raises(UnsafeEgressTarget, match="non-global"):
        await resolve_public_https_target(
            "https://example.com/hook",
            resolver=resolver_for(*addresses),
        )


async def test_resolve_rejects_private_ip_literal_without_dns() -> None:
    async def resolver_must_not_run(_host: str, _port: int):
        raise AssertionError("IP literals must not be sent to DNS")

    with pytest.raises(UnsafeEgressTarget, match="non-global"):
        await resolve_public_https_target(
            "https://127.0.0.1/admin",
            resolver=resolver_must_not_run,
        )


async def test_resolve_deduplicates_and_bounds_dns_answers() -> None:
    duplicate = resolver_for("1.1.1.1", "1.1.1.1", "8.8.8.8")
    target = await resolve_public_https_target(
        "https://example.com",
        resolver=duplicate,
        max_addresses=2,
    )
    assert tuple(str(address) for address in target.addresses) == ("1.1.1.1", "8.8.8.8")

    with pytest.raises(UnsafeEgressTarget, match="more than 1"):
        await resolve_public_https_target(
            "https://example.com",
            resolver=duplicate,
            max_addresses=1,
        )


async def test_open_stream_pins_ip_and_preserves_tls_hostname() -> None:
    target = await resolve_public_https_target(
        "https://example.com/hook",
        resolver=resolver_for("93.184.216.34"),
    )
    calls: list[tuple[str, int, str]] = []
    writer = FakeWriter("93.184.216.34")
    reader = asyncio.StreamReader()

    async def opener(host: str, port: int, **kwargs):
        assert kwargs["ssl"] is not None
        calls.append((host, port, kwargs["server_hostname"]))
        return reader, writer

    opened_reader, opened_writer = await open_verified_tls_stream(target, opener=opener)

    assert (opened_reader, opened_writer) == (reader, writer)
    assert calls == [("93.184.216.34", 443, "example.com")]


async def test_open_stream_falls_back_within_one_budget() -> None:
    target = await resolve_public_https_target(
        "https://example.com/hook",
        resolver=resolver_for("93.184.216.34", "1.1.1.1"),
    )
    attempted: list[str] = []

    async def opener(host: str, _port: int, **_kwargs):
        attempted.append(host)
        if host == "93.184.216.34":
            raise OSError("first route unavailable")
        return asyncio.StreamReader(), FakeWriter(host)

    _reader, writer = await open_verified_tls_stream(target, opener=opener)

    assert attempted == ["93.184.216.34", "1.1.1.1"]
    assert writer.peer_address == "1.1.1.1"


async def test_open_stream_fails_closed_on_peer_mismatch() -> None:
    target = await resolve_public_https_target(
        "https://example.com/hook",
        resolver=resolver_for("93.184.216.34"),
    )
    writer = FakeWriter("10.0.0.8")

    async def opener(*_args, **_kwargs):
        return asyncio.StreamReader(), writer

    with pytest.raises(EgressConnectionError, match="did not match"):
        await open_verified_tls_stream(target, opener=opener)
    assert writer.closed is True


async def test_open_stream_reports_exhausted_addresses() -> None:
    target = await resolve_public_https_target(
        "https://example.com/hook",
        resolver=resolver_for("93.184.216.34"),
    )

    async def opener(*_args, **_kwargs):
        raise OSError("network unavailable")

    with pytest.raises(EgressConnectionError, match="no vetted address"):
        await open_verified_tls_stream(target, opener=opener)
