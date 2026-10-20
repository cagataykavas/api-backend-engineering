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

The Postgres repository applies this contract at two layers. Pool acquisition
cannot wait longer than the remaining request budget, and every repository
operation runs in a transaction with a local `statement_timeout`. The database
budget reserves 25 ms for response handling, rounds down to whole milliseconds,
and fails before acquiring a connection when that reserve has already consumed
the request. Transaction-local configuration prevents one request's timeout from
leaking through the pool into another request. Calls outside an HTTP request keep
the pool's configured command timeout.

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
- PostgreSQL enforces a transaction-local statement ceiling, but the outer
  monotonic deadline remains authoritative across multiple sequential queries.
- This is an end-to-end server processing budget, not a guarantee of client
  network latency or successful response delivery.
- Proxies must preserve exactly one timeout header. Edge and application limits
  should be coordinated so the outermost deadline expires last.
- A production downstream HTTP client should propagate a smaller remaining
  budget using the protocol expected by that dependency.

## Next step

Propagate the remaining budget to outbound HTTP clients, then expose admitted,
expired and remaining-budget histograms without high-cardinality labels.
