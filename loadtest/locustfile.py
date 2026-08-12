from __future__ import annotations

import uuid

from locust import HttpUser, between, task


class ApiUser(HttpUser):
    wait_time = between(0.05, 0.3)

    @task(5)
    def health(self):
        self.client.get("/health", name="GET /health")

    @task(2)
    def create_order(self):
        key = str(uuid.uuid4())
        self.client.post(
            "/v1/orders",
            json={"customer_id": "load-user", "amount": 42.0},
            headers={"Idempotency-Key": key},
            name="POST /v1/orders",
        )

    @task(3)
    def list_orders(self):
        self.client.get("/v1/orders?limit=25", name="GET /v1/orders")
