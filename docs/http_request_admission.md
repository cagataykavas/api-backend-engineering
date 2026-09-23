# HTTP request admission boundary

`RequestBodyAdmissionMiddleware` validates message framing and buffers a bounded request body before a route, dependency, database transaction, or cache mutation can run.

## Contract

The boundary:

- rejects multiple `Content-Length` fields, even when their values agree;
- rejects requests containing both `Content-Length` and `Transfer-Encoding`;
- requires `Content-Length` to contain ASCII decimal digits only;
- rejects a declared or observed body larger than `MAX_REQUEST_BODY_BYTES`;
- checks that the observed body size matches the declared length;
- bounds the number of ASGI receive messages, including empty chunks;
- replays one admitted body to the downstream application; and
- emits stable rejection reasons in JSON and `X-Request-Admission-Reason`.

The default limit is 1 MiB. Configure it at the process boundary:

```bash
MAX_REQUEST_BODY_BYTES=262144 uvicorn app:app
```

Invalid configuration fails during application construction instead of silently disabling the limit.

## Why pre-buffer

Counting chunks only while a route consumes them is too late for a strong admission contract: dependencies or handlers may have already produced side effects. Bounded pre-buffering makes the decision before downstream code runs and also covers bodies without `Content-Length`.

This trade-off is appropriate for the small JSON API in this repository. A streaming upload service should use a different architecture: ingress limits, streaming directly to quarantined object storage, checksum verification, and promotion only after validation.

## Trust boundary and limitations

- The ASGI server and reverse proxy remain responsible for parsing raw HTTP. This middleware is defense in depth and does not claim to detect every request-smuggling technique.
- Configure equivalent or smaller limits at the public ingress. Otherwise the application still spends network capacity receiving a body it will reject.
- The limit applies to bytes delivered by the ASGI server. If another component decompresses request bodies, it must enforce a post-decompression limit as well.
- Pre-buffering consumes up to the configured limit per concurrent admitted request. Concurrency limits and server timeouts are separate controls.
- Rejection bodies are deliberately small and contain no request content.

## Operational next step

Expose rejection counters by reason and correlate them with ingress logs. Alert on sustained ambiguous-framing rejections separately from ordinary `payload_too_large` client errors.
