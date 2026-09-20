from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ClientIdentityPolicy:
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_forwarded_hops: int = 10

    def __post_init__(self) -> None:
        if self.max_forwarded_hops < 1:
            raise ValueError("max_forwarded_hops must be positive")
        for cidr in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=True)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR: {cidr}") from exc


@dataclass(frozen=True)
class ClientIdentity:
    client_ip: str
    source: str
    peer_ip: str
    forwarded_chain: tuple[str, ...]
    ignored_untrusted_forwarding: bool = False

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["forwarded_chain"] = list(self.forwarded_chain)
        return payload


def _address(value: str, *, field: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise ValueError(f"invalid {field} IP address") from exc


def _is_trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def resolve_client_identity(
    peer_ip: str,
    x_forwarded_for: str | None,
    policy: ClientIdentityPolicy | None = None,
) -> ClientIdentity:
    """Resolve a rate-limit identity without trusting arbitrary forwarding headers.

    The chain is evaluated from the immediate peer toward the originating client.
    Only known proxy hops are removed; the first untrusted address is the client.
    """

    active_policy = policy or ClientIdentityPolicy()
    peer = _address(peer_ip, field="peer")
    networks = tuple(
        ipaddress.ip_network(cidr, strict=True) for cidr in active_policy.trusted_proxy_cidrs
    )

    if not x_forwarded_for:
        return ClientIdentity(str(peer), "peer", str(peer), ())

    if not _is_trusted(peer, networks):
        return ClientIdentity(
            str(peer),
            "peer",
            str(peer),
            (),
            ignored_untrusted_forwarding=True,
        )

    raw_hops = [item.strip() for item in x_forwarded_for.split(",")]
    if any(not item for item in raw_hops):
        raise ValueError("forwarded chain contains an empty hop")
    if len(raw_hops) > active_policy.max_forwarded_hops:
        raise ValueError("forwarded chain exceeds max_forwarded_hops")

    forwarded = tuple(_address(item, field="forwarded") for item in raw_hops)
    for address in reversed(forwarded):
        if not _is_trusted(address, networks):
            return ClientIdentity(
                client_ip=str(address),
                source="x_forwarded_for",
                peer_ip=str(peer),
                forwarded_chain=tuple(str(item) for item in forwarded),
            )
    raise ValueError("forwarded chain contains no untrusted client address")
