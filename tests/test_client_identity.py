from __future__ import annotations

import json

import pytest

from backend.client_identity import ClientIdentityPolicy, resolve_client_identity

POLICY = ClientIdentityPolicy(
    trusted_proxy_cidrs=("10.0.0.0/8", "2001:db8:abcd::/48"),
    max_forwarded_hops=4,
)


def test_uses_socket_peer_without_forwarding_header():
    identity = resolve_client_identity("192.0.2.10", None, POLICY)

    assert identity.client_ip == "192.0.2.10"
    assert identity.source == "peer"


def test_ignores_spoofed_header_from_untrusted_peer():
    identity = resolve_client_identity("192.0.2.10", "198.51.100.7", POLICY)

    assert identity.client_ip == "192.0.2.10"
    assert identity.ignored_untrusted_forwarding
    assert identity.forwarded_chain == ()


def test_selects_first_untrusted_hop_before_trusted_proxy_chain():
    identity = resolve_client_identity(
        "10.0.0.5",
        "198.51.100.7, 203.0.113.8, 10.1.2.3",
        POLICY,
    )

    assert identity.client_ip == "203.0.113.8"
    assert identity.source == "x_forwarded_for"
    assert identity.forwarded_chain == ("198.51.100.7", "203.0.113.8", "10.1.2.3")
    assert json.loads(json.dumps(identity.to_dict()))["client_ip"] == "203.0.113.8"


def test_supports_ipv6_peer_and_forwarded_chain():
    identity = resolve_client_identity(
        "2001:db8:abcd::5",
        "2001:db8:ffff::7, 2001:db8:abcd::9",
        POLICY,
    )

    assert identity.client_ip == "2001:db8:ffff::7"


@pytest.mark.parametrize(
    "peer, forwarded, match",
    [
        ("not-an-ip", None, "invalid peer"),
        ("10.0.0.5", "198.51.100.1,,10.0.0.2", "empty hop"),
        ("10.0.0.5", "198.51.100.1,198.51.100.2,198.51.100.3,198.51.100.4,10.0.0.2", "exceeds"),
        ("10.0.0.5", "bad-address", "invalid forwarded"),
        ("10.0.0.5", "10.0.0.1,10.0.0.2", "no untrusted client"),
    ],
)
def test_rejects_ambiguous_or_malformed_trusted_proxy_evidence(peer, forwarded, match):
    with pytest.raises(ValueError, match=match):
        resolve_client_identity(peer, forwarded, POLICY)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trusted_proxy_cidrs": ("10.0.0.1/8",)},
        {"trusted_proxy_cidrs": ("not-a-network",)},
        {"max_forwarded_hops": 0},
    ],
)
def test_rejects_invalid_policy(kwargs):
    with pytest.raises(ValueError):
        ClientIdentityPolicy(**kwargs)
