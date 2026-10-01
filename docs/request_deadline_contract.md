# Request deadline contract

The API admits one bounded processing budget for every HTTP request. Clients
may request a shorter or longer budget with `X-Request-Timeout-Ms`; the server
uses 2,000 ms by default and rejects values outside 1–10,000 ms.

Deployments can set the canonical positive integer environment variables
`REQUEST_TIMEOUT_MS` and `MAX_REQUEST_TIMEOUT_MS`. Invalid configuration fails
application construction instead of silently disabling the guard.

```http
POST /v1/orders HTTP/1.1
X-Request-Timeout-Ms: 750
Content-Type: application/json
```

The value must be canonical positive decimal milliseconds. Duplicate headers,
signs, whitespace, fractions, zero and out-of-policy values return a bounded
`400 application/problem+json` response before route dispatch. Successful
responses echo the admitted budget in the same header.

## Cancellation and propagation

The middleware establishes a monotonic deadline in a `ContextVar` and wraps the
complete ASGI application call in `asyncio.timeout_at`. If the deadline expires
before response headers are sent, the current request task is cancelled and a
`504 application/problem+json` response is returned. Cancellation therefore
reaches awaited asyncpg, Redis and downstream HTTP operations rather than
letting abandoned work continue in the background.

Downstream adapters can derive their own smaller timeout without parsing the
transport header again:

```python
from backend.request_deadline import remaining_request_seconds

timeout = remaining_request_seconds(reserve_ms=25)
```

The reserve leaves time for serialization and response delivery. Outside a
request the helper returns `None`; inside a request it returns a monotonic,
non-negative value.

If a streaming response has already emitted headers, HTTP cannot safely replace
its status with `504`. In that case the middleware cancels the application and
raises, causing the server to terminate the incomplete stream instead of
emitting a second response. If the response body is already complete, expiry
only cancels remaining post-response work and does not manufacture a server
error after the client has received a valid response.

## Operational limits

- Timeout cancellation is cooperative: blocking synchronous code must be
  isolated separately and may not stop immediately.
- A database or external service can commit before cancellation arrives.
  Mutating endpoints still require idempotency and transactional boundaries.
- This is an end-to-end server processing budget, not a guarantee of client
  network latency or successful response delivery.
- Proxies must preserve exactly one timeout header. Edge and application limits
  should be coordinated so the outermost deadline expires last.
- A production downstream HTTP client should propagate a smaller remaining
  budget using the protocol expected by that dependency.

## Next step

Apply `remaining_request_seconds()` directly to asyncpg statement timeouts and
outbound HTTP client calls, then expose admitted, expired and remaining-budget
histograms without high-cardinality labels.
