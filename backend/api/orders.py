from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from backend.api.schemas import OrderCreate
from backend.errors import IdempotencyConflict, InvalidCursor
from backend.services.orders import OrderService


router = APIRouter(prefix="/v1/orders", tags=["orders"])


def get_order_service(request: Request) -> OrderService:
    return request.app.state.order_service


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_order(
    request: Request,
    response: Response,
    payload: OrderCreate,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
    x_client_id: Annotated[
        str,
        Header(alias="X-Client-ID"),
    ] = "anonymous",
):
    request.app.state.rate_limit(x_client_id)
    service = get_order_service(request)
    try:
        order, replayed = await service.create(
            payload,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return order


@router.get("/{order_id}")
async def get_order(request: Request, order_id: str):
    service = get_order_service(request)
    order = await service.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@router.get("")
async def list_orders(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    customer_id: str | None = Query(None, min_length=1, max_length=80),
):
    service = get_order_service(request)
    try:
        return await service.list_orders(
            customer_id=customer_id,
            limit=limit,
            cursor=cursor,
        )
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
