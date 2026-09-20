# Trusted proxy client identity

Rate limiting by a caller-supplied identifier is bypassable: a client can rotate
the header on every request. `backend.client_identity` resolves a stable network
identity from the socket peer and accepts `X-Forwarded-For` only when the
immediate peer belongs to an explicitly trusted proxy CIDR.

```python
policy = ClientIdentityPolicy(
    trusted_proxy_cidrs=("10.20.0.0/16", "2001:db8:abcd::/48"),
    max_forwarded_hops=4,
)
identity = resolve_client_identity(request.client.host, request.headers.get("x-forwarded-for"), policy)
rate_limiter.check(identity.client_ip)
```

The resolver walks the forwarding chain from right to left, removes only trusted
proxy hops, and selects the first untrusted address. A forwarding header received
directly from an untrusted peer is ignored and recorded in the decision. Invalid,
empty, overlong, or all-trusted chains fail closed. IPv4 and IPv6 CIDRs are
handled independently, and the result is JSON-ready for audit logging.

## Deployment contract and limits

Configure CIDRs from the actual load-balancer or ingress network, not broad
private ranges. The edge proxy must replace, rather than append to, forwarding
headers received from the public client. This utility does not authenticate an
application user, parse RFC 7239 `Forwarded`, or prove that a trusted proxy was
configured correctly. Distributed rate limiting still requires shared state
such as Redis and atomic server-side updates.
