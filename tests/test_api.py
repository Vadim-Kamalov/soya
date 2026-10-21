import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest


def test_registration_defaults_role(client):
    result = client.post("/auth/register", json={"username": "default_role", "password": "password123"})
    assert result.status_code == 201, result.text
    assert result.json()["role"] == "USER"


def test_auth_rotation_and_token_types(client, account):
    user = account()
    pair = user["tokens"]
    assert pair["expires_in"] == 1200
    assert client.get("/products").json()["error_code"] == "TOKEN_INVALID"
    assert (
        client.get("/products", headers={"Authorization": "Bearer " + pair["refresh_token"]}).status_code
        == 401
    )
    new = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert new.status_code == 200
    assert new.json()["refresh_token"] != pair["refresh_token"]
    assert (
        client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]}).json()["error_code"]
        == "REFRESH_TOKEN_INVALID"
    )
    assert client.post("/auth/refresh", json={"refresh_token": pair["access_token"]}).status_code == 401
    assert (
        client.post("/auth/login", json={"username": user["username"], "password": "incorrect"}).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/register", json={"username": "hacker", "password": "password123", "role": "ADMIN"}
        ).status_code
        == 400
    )


def test_expired_and_malformed_tokens(client, account, monkeypatch):
    user = account()
    payload = jwt.decode(user["tokens"]["access_token"], options={"verify_signature": False})
    payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    expired = jwt.encode(payload, "test-only-secret-at-least-thirty-two-characters", algorithm="HS256")
    assert (
        client.get("/products", headers={"Authorization": "Bearer " + expired}).json()["error_code"]
        == "TOKEN_EXPIRED"
    )
    for token in ["abc", jwt.encode({"sub": "bad"}, "wrong-key", algorithm="HS256")]:
        assert (
            client.get("/products", headers={"Authorization": "Bearer " + token}).json()["error_code"]
            == "TOKEN_INVALID"
        )


def test_product_crud_pagination_ownership_and_soft_delete(client, account, product, sql):
    user, other, admin = account(), account("SELLER"), account("ADMIN")
    p = product()
    assert isinstance(p["price"], (int, float))
    assert p["created_at"] and p["updated_at"]
    assert client.get("/products/" + p["id"], headers=user["headers"]).status_code == 200
    assert (
        client.post(
            "/products",
            headers=user["headers"],
            json={
                "name": "x",
                "price": 1,
                "stock": 0,
                "category": "x",
                "status": "ACTIVE",
            },
        ).status_code
        == 403
    )
    assert (
        client.delete("/products/" + p["id"], headers=other["headers"]).json()["error_code"]
        == "ACCESS_DENIED"
    )
    payload = {"name": "Updated", "price": 20, "stock": 3, "category": "BOOKS", "status": "INACTIVE"}
    updated = client.put("/products/" + p["id"], headers=admin["headers"], json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["updated_at"] > p["updated_at"]
    for category, expected in [("books", 0), ("BOOKS", 1)]:
        page = client.get(
            "/products", headers=user["headers"], params={"category": category, "size": 1}
        ).json()
        assert page["totalElements"] == expected
        assert page["page"] == 0 and page["size"] == 1
    assert client.delete("/products/" + p["id"], headers=admin["headers"]).status_code == 204
    assert sql("SELECT status FROM products WHERE id=%s", (p["id"],))[0]["status"] == "ARCHIVED"
    assert (
        client.get("/products/" + str(uuid4()), headers=user["headers"]).json()["error_code"]
        == "PRODUCT_NOT_FOUND"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "x" * 256),
        ("description", "x" * 4001),
        ("price", 0),
        ("price", 0.001),
        ("stock", -1),
        ("stock", 1.5),
        ("stock", True),
        ("category", ""),
        ("category", "x" * 101),
        ("status", "UNKNOWN"),
        ("name", "a\u0000b"),
        ("description", "a\u0000b"),
        ("category", "a\u0000b"),
    ],
)
def test_contract_validation(client, account, sql, field, value):
    seller = account("SELLER")
    data = dict(name="x", price=1, stock=1, category="x", status="ACTIVE") | {field: value}
    response = client.post("/products", headers=seller["headers"], json=data)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "VALIDATION_ERROR"
    assert any(field in x["field"] for x in response.json()["details"]["fields"])
    assert sql("SELECT count(*) AS n FROM products")[0]["n"] == 0


@pytest.mark.parametrize("field", ["name", "description", "category"])
def test_product_update_rejects_nul_without_changing_record(client, account, product, sql, field):
    p, admin = product(), account("ADMIN")
    payload = dict(name="Book", price=100, stock=10, category="books", status="ACTIVE")
    result = client.put(
        "/products/" + p["id"], headers=admin["headers"], json=payload | {field: "a\u0000b"}
    )
    assert result.status_code == 400, result.text
    assert result.json()["error_code"] == "VALIDATION_ERROR"
    assert any(field in item["field"] for item in result.json()["details"]["fields"])
    stored = sql("SELECT name,description,category,stock FROM products WHERE id=%s", (p["id"],))[0]
    assert stored == dict(name="Book", description=None, category="books", stock=10)


def test_category_filter_rejects_nul(client, account):
    result = client.get("/products", headers=account()["headers"], params={"category": "a\u0000b"})
    assert result.status_code == 400, result.text
    assert result.json()["error_code"] == "VALIDATION_ERROR"
    assert result.json()["details"]["fields"][0]["field"] == "query.category"


def test_query_path_json_validation_and_error_envelope(client, account):
    headers = account()["headers"]
    for path in ["/products?page=-1", "/products?size=0", "/products?status=BAD", "/products/not-a-uuid"]:
        response = client.get(path, headers=headers)
        assert response.status_code == 400
        assert response.json()["details"]["fields"]
    response = client.post("/orders", headers=headers | {"Content-Type": "application/json"}, content="{bad")
    assert response.status_code == 400
    assert client.get("/does-not-exist").json()["error_code"] == "ROUTE_NOT_FOUND"


def test_json_request_logs_mask_secrets(client, account, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="marketplace.api")
    user = account()
    response = client.get("/products", headers=user["headers"])
    UUID(response.headers["X-Request-Id"])
    events = [json.loads(record.message) for record in caplog.records if record.name == "marketplace.api"]
    event = next(x for x in events if x["request_id"] == response.headers["X-Request-Id"])
    assert event["user_id"] == user["id"] and event["status_code"] == 200
    assert {
        "request_id",
        "method",
        "endpoint",
        "status_code",
        "duration_ms",
        "user_id",
        "timestamp",
    } <= event.keys()
    assert "password123" not in json.dumps(events)
    assert any(x.get("request_body", {}).get("password") == "***" for x in events)
