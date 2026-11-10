from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, TypeAlias
from urllib.parse import SplitResult, urlsplit

IpAddress: TypeAlias = IPv4Address | IPv6Address
AddressInfo: TypeAlias = tuple[int, int, int, str, tuple[Any, ...]]
Resolver: TypeAlias = Callable[[str, int], Awaitable[Sequence[AddressInfo]]]
StreamOpener: TypeAlias = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class UnsafeEgressTarget(ValueError):
    """Raised when an outbound destination does not satisfy the egress policy."""


class EgressConnectionError(ConnectionError):
    """Raised when no policy-approved address can be reached safely."""


@dataclass(frozen=True, slots=True)
class VerifiedHttpsTarget:
    """An HTTPS destination whose DNS snapshot passed the public-address policy."""

    url: str
    host: str
    port: int
    request_target: str
    addresses: tuple[IpAddress, ...]


async def _system_resolver(host: str, port: int) -> Sequence[AddressInfo]:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )


def _canonical_host(parts: SplitResult) -> str:
    if parts.username is not None or parts.password is not None:
        raise UnsafeEgressTarget("credentials are not allowed in egress URLs")
    if parts.hostname is None:
        raise UnsafeEgressTarget("egress URL must include a host")

    raw_host = parts.hostname.rstrip(".")
    if not raw_host:
        raise UnsafeEgressTarget("egress URL must include a host")
    try:
        canonical = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeEgressTarget("egress URL contains an invalid host") from exc
    if len(canonical) > 253 or any(len(label) > 63 for label in canonical.split(".")):
        raise UnsafeEgressTarget("egress URL host exceeds DNS length limits")
    return canonical


def _parse_https_url(url: str, allowed_ports: frozenset[int]) -> tuple[str, int, str]:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UnsafeEgressTarget("only https egress URLs are allowed")
    if parts.fragment:
        raise UnsafeEgressTarget("fragments are not allowed in egress URLs")

    host = _canonical_host(parts)
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeEgressTarget("egress URL contains an invalid port") from exc
    if port not in allowed_ports:
        raise UnsafeEgressTarget(f"egress port {port} is not allowed")

    path = parts.path or "/"
    request_target = f"{path}?{parts.query}" if parts.query else path
    return host, port, request_target


def _literal_address(host: str) -> IpAddress | None:
    try:
        return ip_address(host)
    except ValueError:
        return None


def _collect_addresses(
    records: Sequence[AddressInfo],
    *,
    max_addresses: int,
) -> tuple[IpAddress, ...]:
    addresses: list[IpAddress] = []
    seen: set[IpAddress] = set()
    for family, _type, _proto, _canonname, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        try:
            address = ip_address(sockaddr[0])
        except ValueError as exc:
            raise UnsafeEgressTarget("resolver returned an invalid IP address") from exc
        if address in seen:
            continue
        seen.add(address)
        addresses.append(address)
        if len(addresses) > max_addresses:
            raise UnsafeEgressTarget(
                f"resolver returned more than {max_addresses} unique addresses"
            )

    if not addresses:
        raise UnsafeEgressTarget("resolver returned no usable IP addresses")
    unsafe = [str(address) for address in addresses if not address.is_global]
    if unsafe:
        raise UnsafeEgressTarget(
            "resolver returned non-global addresses: " + ", ".join(unsafe)
        )
    return tuple(addresses)


async def resolve_public_https_target(
    url: str,
    *,
    resolver: Resolver = _system_resolver,
    allowed_ports: frozenset[int] = frozenset({443}),
    max_addresses: int = 16,
) -> VerifiedHttpsTarget:
    """Resolve an HTTPS URL once and reject the full answer set if any IP is unsafe."""

    if not allowed_ports:
        raise ValueError("allowed_ports must not be empty")
    if max_addresses < 1:
        raise ValueError("max_addresses must be positive")

    host, port, request_target = _parse_https_url(url, allowed_ports)
    literal = _literal_address(host)
    if literal is None:
        records = await resolver(host, port)
        addresses = _collect_addresses(records, max_addresses=max_addresses)
    else:
        addresses = _collect_addresses(
            [(socket.AF_INET6 if literal.version == 6 else socket.AF_INET, 0, 0, "", (host, port))],
            max_addresses=max_addresses,
        )
    return VerifiedHttpsTarget(
        url=url,
        host=host,
        port=port,
        request_target=request_target,
        addresses=addresses,
    )


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def open_verified_tls_stream(
    target: VerifiedHttpsTarget,
    *,
    timeout_seconds: float = 3.0,
    ssl_context: ssl.SSLContext | None = None,
    opener: StreamOpener = asyncio.open_connection,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open TLS to a vetted IP, retain hostname verification, and pin the peer address."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    context = ssl_context or ssl.create_default_context()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_error: BaseException | None = None

    for address in target.addresses:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            reader, writer = await asyncio.wait_for(
                opener(
                    str(address),
                    target.port,
                    ssl=context,
                    server_hostname=target.host,
                ),
                timeout=remaining,
            )
        except (OSError, TimeoutError) as exc:
            last_error = exc
            continue

        peername = writer.get_extra_info("peername")
        try:
            peer_address = ip_address(peername[0]) if peername else None
        except (ValueError, TypeError):
            peer_address = None
        if peer_address != address:
            await _close_writer(writer)
            raise EgressConnectionError("TLS peer address did not match the vetted address")
        return reader, writer

    message = "no vetted address could be reached within the connection budget"
    raise EgressConnectionError(message) from last_error
