from fastapi.testclient import TestClient

from app import IDEMPOTENCY, ITEMS, REQUESTS, app

client = TestClient(app)


def setup_function() -> None:
    ITEMS.clear()
    IDEMPOTENCY.clear()
    REQUESTS.clear()


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_idempotent_create_replays_same_response() -> None:
    headers = {"Idempotency-Key": "abc-123", "X-Client-ID": "pytest"}
    payload = {"name": "risk-score", "value": 0.42}

    first = client.post("/v1/items", json=payload, headers=headers)
    second = client.post("/v1/items", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert len(ITEMS) == 1


def test_idempotency_key_rejects_changed_payload() -> None:
    headers = {"Idempotency-Key": "same-key", "X-Client-ID": "pytest"}
    client.post("/v1/items", json={"name": "a", "value": 1}, headers=headers)
    response = client.post("/v1/items", json={"name": "b", "value": 2}, headers=headers)

    assert response.status_code == 409


def test_cursor_pagination() -> None:
    for index in range(5):
        response = client.post(
            "/v1/items",
            json={"name": f"item-{index}", "value": float(index)},
            headers={"X-Client-ID": f"client-{index}"},
        )
        assert response.status_code == 201

    page1 = client.get("/v1/items?limit=2&cursor=0").json()
    page2 = client.get(f"/v1/items?limit=2&cursor={page1['next_cursor']}").json()

    assert [item["id"] for item in page1["items"]] == [1, 2]
    assert [item["id"] for item in page2["items"]] == [3, 4]
