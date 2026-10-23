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

The pooled `DeadlineHttpClient` applies the same contract to outbound HTTP. It:

- pins one HTTP(S) dependency authority and accepts only origin-relative paths;
- caps every call by the smaller of the remaining request time and the adapter maximum;
- wraps the complete send/read operation in one `asyncio.timeout`, because HTTPX's phase
  timeouts alone are not an end-to-end wall-clock budget;
- also sets bounded connect, pool, read and write phase timeouts;
- reserves time for the API response and sends a still-smaller canonical
  `X-Request-Timeout-Ms` value to the dependency;
- rejects caller overrides of the destination host, framing or timeout header;
- returns redirects without following them to another authority;
- fails before connection-pool acquisition when the dispatch minimum is unavailable; and
- maps transport timeout details to a stable, non-sensitive error code.

Outside an admitted API request, the adapter uses its own bounded default budget. Callers must
create one client per dependency during application lifespan and close it at shutdown so connection
pooling is preserved without leaking sockets.

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
- HTTP cancellation is cooperative and cannot prove that a remote mutation did not commit.
  Mutating dependency calls still require idempotency keys and reconciliation.
- The adapter currently buffers HTTPX responses and does not impose a response-byte ceiling;
  large or streaming dependencies require a separate bounded streaming contract.
- DNS behavior remains dependent on the configured HTTPX/httpcore resolver and operating system.

## Next step

Expose admitted, exhausted and completed outbound calls plus remaining-budget histograms without
high-cardinality URL labels, then add a bounded streaming-response contract.
