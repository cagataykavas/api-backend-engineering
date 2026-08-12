from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from telemetry import LATENCY, REQUESTS, configure_telemetry

app = FastAPI(title="API Backend Engineering Lab", version="1.1.0")
configure_telemetry(app)

ITEMS: dict[int, dict] = {}
IDEMPOTENCY: dict[str, dict] = {}
REQUEST_HISTORY: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT = 30
WINDOW_SECONDS = 60


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    value: float


def rate_limit(client_id: str) -> None:
    now = time.time()
    bucket = REQUEST_HISTORY[client_id]
    while bucket and bucket[0] <= now - WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    bucket.append(now)


def next_id() -> int:
    return max(ITEMS, default=0) + 1


@app.middleware("http")
async def record_metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/items", status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ItemCreate,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    x_client_id: Annotated[str, Header(alias="X-Client-ID")] = "anonymous",
):
    rate_limit(x_client_id)
    body_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    if idempotency_key:
        prior = IDEMPOTENCY.get(idempotency_key)
        if prior:
            if prior["body_hash"] != body_hash:
                raise HTTPException(status_code=409, detail="idempotency key reused with different payload")
            return prior["response"]

    item_id = next_id()
    response = {"id": item_id, **payload.model_dump()}
    ITEMS[item_id] = response
    if idempotency_key:
        IDEMPOTENCY[idempotency_key] = {"body_hash": body_hash, "response": response}
    return response


@app.get("/v1/items")
def list_items(
    limit: int = Query(20, ge=1, le=100),
    cursor: int = Query(0, ge=0),
):
    ids = sorted(i for i in ITEMS if i > cursor)[:limit]
    results = [ITEMS[i] for i in ids]
    next_cursor = ids[-1] if len(ids) == limit else None
    return {"items": results, "next_cursor": next_cursor}
