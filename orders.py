"""Compatibility imports for the pre-package portfolio API.

New code should import from ``backend.*``. This module remains intentionally thin so
existing examples and external links do not break during the package migration.
"""

from backend.api.schemas import OrderCreate
from backend.contracts import JsonCache, OrderStore
from backend.domain.orders import CreateOrderResult, OrderRecord, OrderStatus
from backend.errors import IdempotencyConflict, InvalidCursor
from backend.repositories.memory import InMemoryOrderStore
from backend.services.orders import (
    OrderService,
    canonical_payload_hash,
    decode_cursor,
    encode_cursor,
    order_to_dict,
)

__all__ = [
    "CreateOrderResult",
    "IdempotencyConflict",
    "InMemoryOrderStore",
    "InvalidCursor",
    "JsonCache",
    "OrderCreate",
    "OrderRecord",
    "OrderService",
    "OrderStatus",
    "OrderStore",
    "canonical_payload_hash",
    "decode_cursor",
    "encode_cursor",
    "order_to_dict",
]
