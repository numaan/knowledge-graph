from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_create_order():
    r = client.post("/api/orders", json={"customer_id": 1, "lines": []})
    assert r.status_code == 201


def test_get_order():
    assert client.get("/api/orders/42").status_code == 200
