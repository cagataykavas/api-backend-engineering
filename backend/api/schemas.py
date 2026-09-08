from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    customer_id: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0, le=1_000_000_000)
