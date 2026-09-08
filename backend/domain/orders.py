from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderStatus(StrEnum):
    CREATED = "created"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    customer_id: str
    amount: float
    status: OrderStatus
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("order_id must not be empty")
        if not self.customer_id:
            raise ValueError("customer_id must not be empty")
        if self.amount < 0:
            raise ValueError("amount must be non-negative")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    order: OrderRecord
    replayed: bool
