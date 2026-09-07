from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import IDEMPOTENCY, ITEMS, REQUEST_HISTORY, app


@pytest.fixture
def client():
    ITEMS.clear()
    IDEMPOTENCY.clear()
    REQUEST_HISTORY.clear()
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_idempotent_create_replays_same_response(client: TestClient) -> None:
    headers = {"Idempotency-Key": "abc-123", "X-Client-ID": "pytest"}
    payload = {"name": "risk-score", "value": 0.42}

    first = client.post("/v1/items", json=payload, headers=headers)
    second = client.post("/v1/items", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert len(ITEMS) == 1


def test_idempotency_key_rejects_changed_payload(client: TestClient) -> None:
    headers = {"Idempotency-Key": "same-key", "X-Client-ID": "pytest"}
    client.post("/v1/items", json={"name": "a", "value": 1}, headers=headers)
    response = client.post(
        "/v1/items",
        json={"name": "b", "value": 2},
        headers=headers,
    )

    assert response.status_code == 409


def test_cursor_pagination(client: TestClient) -> None:
    for index in range(5):
        response = client.post(
            "/v1/items",
            json={"name": f"item-{index}", "value": float(index)},
            headers={"X-Client-ID": f"client-{index}"},
        )
        assert response.status_code == 201

    page1 = client.get("/v1/items?limit=2&cursor=0").json()
    page2 = client.get(
        f"/v1/items?limit=2&cursor={page1['next_cursor']}"
    ).json()

    assert [item["id"] for item in page1["items"]] == [1, 2]
    assert [item["id"] for item in page2["items"]] == [3, 4]


def test_order_idempotency_replays_original_order(client: TestClient) -> None:
    headers = {"Idempotency-Key": "order-key", "X-Client-ID": "orders-test"}
    payload = {"customer_id": "customer-1", "amount": 42.5}

    first = client.post("/v1/orders", json=payload, headers=headers)
    second = client.post("/v1/orders", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"


def test_order_idempotency_conflict_is_explicit(client: TestClient) -> None:
    headers = {"Idempotency-Key": "conflict-key", "X-Client-ID": "orders-conflict"}
    client.post(
        "/v1/orders",
        json={"customer_id": "customer-2", "amount": 10},
        headers=headers,
    )
    response = client.post(
        "/v1/orders",
        json={"customer_id": "customer-2", "amount": 11},
        headers=headers,
    )

    assert response.status_code == 409
    assert "different payload" in response.json()["detail"]


def test_order_get_and_cursor_pagination(client: TestClient) -> None:
    created_ids: set[str] = set()
    for index in range(4):
        response = client.post(
            "/v1/orders",
            json={"customer_id": "paged-customer", "amount": index + 1},
            headers={"X-Client-ID": f"order-page-{index}"},
        )
        assert response.status_code == 201
        created_ids.add(response.json()["order_id"])

    first_page = client.get(
        "/v1/orders",
        params={"customer_id": "paged-customer", "limit": 2},
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert len(first_payload["orders"]) == 2
    assert first_payload["next_cursor"] is not None

    second_page = client.get(
        "/v1/orders",
        params={
            "customer_id": "paged-customer",
            "limit": 2,
            "cursor": first_payload["next_cursor"],
        },
    )
    assert second_page.status_code == 200
    all_orders = first_payload["orders"] + second_page.json()["orders"]
    assert {order["order_id"] for order in all_orders} == created_ids

    order_id = all_orders[0]["order_id"]
    get_response = client.get(f"/v1/orders/{order_id}")
    assert get_response.status_code == 200
    assert get_response.json()["order_id"] == order_id


def test_order_invalid_cursor_returns_400(client: TestClient) -> None:
    response = client.get("/v1/orders?cursor=not-a-real-cursor")
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid pagination cursor"


def test_missing_order_returns_404(client: TestClient) -> None:
    response = client.get("/v1/orders/does-not-exist")
    assert response.status_code == 404
