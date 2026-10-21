from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest


def place(client, user, p, quantity=2, **extra):
    return client.post(
        "/orders",
        headers=user["headers"],
        json={
            "items": [{"product_id": p["id"], "quantity": quantity}],
            **extra,
        },
    )


def promo(client, account, **overrides):
    admin = account("ADMIN")
    now = datetime.now(UTC)
    body = (
        dict(
            code="PROMO_" + uuid4().hex[:6].upper(),
            discount_type="PERCENTAGE",
            discount_value=10,
            min_order_amount=100,
            max_uses=2,
            valid_from=(now - timedelta(days=1)).isoformat(),
            valid_until=(now + timedelta(days=1)).isoformat(),
        )
        | overrides
    )
    response = client.post("/promo-codes", headers=admin["headers"], json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_snapshot_update_cancel_atomicity(client, account, product, sql):
    user = account()
    p = product()
    order = place(client, user, p)
    assert order.status_code == 201, order.text
    order = order.json()
    assert order["total_amount"] == 200
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 8
    sql("UPDATE products SET price=150 WHERE id=%s", (p["id"],))
    fetched = client.get("/orders/" + order["id"], headers=user["headers"]).json()
    assert fetched["items"][0]["price_at_order"] == 100
    failed = client.put(
        "/orders/" + order["id"],
        headers=user["headers"],
        json={
            "items": [{"product_id": p["id"], "quantity": 11}],
        },
    )
    assert failed.status_code == 409 and failed.json()["error_code"] == "INSUFFICIENT_STOCK"
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 8
    assert sql("SELECT quantity FROM order_items")[0]["quantity"] == 2
    assert sql("SELECT count(*) AS n FROM user_operations WHERE operation_type='UPDATE_ORDER'")[0]["n"] == 0
    updated = client.put(
        "/orders/" + order["id"],
        headers=user["headers"],
        json={
            "items": [{"product_id": p["id"], "quantity": 3}],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["total_amount"] == 300
    assert updated.json()["items"][0]["price_at_order"] == 100
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 7
    assert client.post("/orders/" + order["id"] + "/cancel", headers=user["headers"]).status_code == 200
    assert (
        client.post("/orders/" + order["id"] + "/cancel", headers=user["headers"]).json()["error_code"]
        == "INVALID_STATE_TRANSITION"
    )
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 10


def test_order_ownership_roles_and_active_limit(client, account, product):
    user, other, seller, admin = account(), account(), account("SELLER"), account("ADMIN")
    p = product()
    order = place(client, user, p).json()
    assert place(client, user, p).json()["error_code"] == "ORDER_HAS_ACTIVE"
    for method, path, body in [
        ("get", "", None),
        ("put", "", {"items": [{"product_id": p["id"], "quantity": 1}]}),
        ("post", "/cancel", None),
    ]:
        response = client.request(
            method, "/orders/" + order["id"] + path, headers=other["headers"], json=body
        )
        assert response.json()["error_code"] == "ORDER_OWNERSHIP_VIOLATION"
    assert client.get("/orders/" + order["id"], headers=admin["headers"]).status_code == 200
    assert client.get("/orders/" + order["id"], headers=seller["headers"]).status_code == 403
    assert place(client, seller, p).status_code == 403
    assert (
        client.get("/orders/" + str(uuid4()), headers=user["headers"]).json()["error_code"]
        == "ORDER_NOT_FOUND"
    )


def test_rate_limit_order_of_checks(client, account, product, monkeypatch, sql):
    user = account()
    p = product()
    order = place(client, user, p).json()
    monkeypatch.setenv("ORDER_RATE_LIMIT_MINUTES", "1")
    assert place(client, user, p).json()["error_code"] == "ORDER_LIMIT_EXCEEDED"

    def put():
        return client.put(
            "/orders/" + order["id"],
            headers=user["headers"],
            json={"items": [{"product_id": p["id"], "quantity": 1}]},
        )

    assert put().status_code == 200
    assert put().json()["error_code"] == "ORDER_LIMIT_EXCEEDED"
    sql("UPDATE user_operations SET created_at=now()-interval '2 minutes'")
    assert put().status_code == 200
    assert place(client, user, p).json()["error_code"] == "ORDER_HAS_ACTIVE"


def test_catalog_before_stock_and_duplicate_product_ids(client, account, product, sql):
    user = account()
    p = product(stock=3)
    inactive = product(status="INACTIVE")
    response = client.post(
        "/orders",
        headers=user["headers"],
        json={
            "items": [
                {"product_id": p["id"], "quantity": 4},
                {"product_id": inactive["id"], "quantity": 1},
            ]
        },
    )
    assert response.json()["error_code"] == "PRODUCT_INACTIVE"
    duplicate = client.post(
        "/orders",
        headers=user["headers"],
        json={
            "items": [
                {"product_id": p["id"], "quantity": 2},
                {"product_id": p["id"], "quantity": 2},
            ]
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["details"]["products"][0] == {
        "product_id": p["id"],
        "requested": 4,
        "available": 3,
    }
    assert sql("SELECT count(*) AS n FROM orders")[0]["n"] == 0
    assert place(client, user, {"id": str(uuid4())}).json()["error_code"] == "PRODUCT_NOT_FOUND"


def test_promo_minimum_rollback_and_usage_return(client, account, product, sql):
    user, other = account(), account()
    p = product()
    code = promo(client, account, max_uses=1, min_order_amount=150)
    failed = place(client, user, p, 1, promo_code=code["code"])
    assert failed.json()["error_code"] == "PROMO_CODE_MIN_AMOUNT"
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 10
    order = place(client, user, p, 2, promo_code=code["code"]).json()
    assert order["discount_amount"] == 20 and order["total_amount"] == 180
    assert place(client, other, p, 2, promo_code=code["code"]).json()["error_code"] == "PROMO_CODE_INVALID"
    # Updating own use succeeds even when the code is at max_uses.
    updated = client.put(
        "/orders/" + order["id"],
        headers=user["headers"],
        json={
            "items": [{"product_id": p["id"], "quantity": 3}],
        },
    )
    assert updated.status_code == 200 and updated.json()["discount_amount"] == 30
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 1
    removed = client.put(
        "/orders/" + order["id"],
        headers=user["headers"],
        json={
            "items": [{"product_id": p["id"], "quantity": 1}],
        },
    ).json()
    assert removed["promo_code_id"] is None and removed["discount_amount"] == 0
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 0
    other_order = place(client, other, p, 2, promo_code=code["code"]).json()
    client.post("/orders/" + other_order["id"] + "/cancel", headers=other["headers"])
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 0


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("PERCENTAGE", 90, 0),
        ("PERCENTAGE", 70, 140),
        ("PERCENTAGE", 70.01, 0),
        ("PERCENTAGE", 100, 0),
        ("FIXED_AMOUNT", 300, 200),
        ("PERCENTAGE", 12.34, 24.68),
    ],
)
def test_discount_caps_and_decimal_rounding(client, account, product, kind, value, expected):
    p = product()
    code = promo(client, account, discount_type=kind, discount_value=value)
    response = place(client, account(), p, promo_code=code["code"])
    assert response.status_code == 201, response.text
    assert Decimal(str(response.json()["discount_amount"])) == Decimal(str(expected))


def test_percentage_rounding_does_not_exceed_seventy_percent(client, account, product):
    p = product(price=0.01)
    code = promo(client, account, discount_value=70, min_order_amount=0)
    response = place(client, account(), p, quantity=1, promo_code=code["code"])
    assert response.status_code == 201
    assert response.json()["discount_amount"] == 0
    assert response.json()["total_amount"] == 0.01


@pytest.mark.parametrize("operation", ["create", "update"])
def test_large_subtotal_is_allowed_when_discounted_amounts_fit(client, account, product, sql, operation):
    p, user = product(price=3000000000), account()
    code = promo(client, account, discount_value=50)
    if operation == "create":
        result = place(client, user, p, quantity=4, promo_code=code["code"])
    else:
        order = place(client, user, p, quantity=2, promo_code=code["code"]).json()
        result = client.put(
            "/orders/" + order["id"], headers=user["headers"],
            json={"items": [{"product_id": p["id"], "quantity": 4}]},
        )
    assert result.status_code == (201 if operation == "create" else 200), result.text
    assert result.json()["total_amount"] == 6000000000
    assert result.json()["discount_amount"] == 6000000000
    stored = sql("SELECT total_amount,discount_amount FROM orders")[0]
    assert stored == dict(total_amount=Decimal("6000000000"), discount_amount=Decimal("6000000000"))
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 6
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 1


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("percentage,quantity", [(None, 4), (10, 4), (70, 10)])
def test_unrepresentable_final_amounts_roll_back(
    client, account, product, sql, operation, percentage, quantity
):
    p, user = product(price=3000000000), account()
    code = promo(client, account, discount_value=percentage) if percentage else None
    extra = {"promo_code": code["code"]} if code else {}
    if operation == "create":
        result = place(client, user, p, quantity=quantity, **extra)
    else:
        original = place(client, user, p, quantity=1, **extra).json()
        result = client.put(
            "/orders/" + original["id"], headers=user["headers"],
            json={"items": [{"product_id": p["id"], "quantity": quantity}]},
        )
        fetched = client.get("/orders/" + original["id"], headers=user["headers"]).json()
        assert fetched == original
    assert result.status_code == 400, result.text
    assert result.json()["error_code"] == "VALIDATION_ERROR"
    existing = int(operation == "update")
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 10 - existing
    assert sql("SELECT count(*) AS n FROM orders")[0]["n"] == existing
    assert sql("SELECT count(*) AS n FROM user_operations")[0]["n"] == existing
    if code:
        assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == existing


def test_update_keeps_existing_snapshots_and_prices_new_items(client, account, product, sql):
    p, new, user = product(price=100), product(price=250), account()
    body = {"items": [{"product_id": p["id"], "quantity": 2}]}
    order = client.post("/orders", headers=user["headers"], json=body).json()
    path = "/orders/" + order["id"]
    sql("UPDATE products SET price=150 WHERE id=%s", (p["id"],))
    unchanged = client.put(path, headers=user["headers"], json=body)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["total_amount"] == 200
    assert unchanged.json()["items"][0]["price_at_order"] == 100
    mixed = {"items": [
        {"product_id": p["id"], "quantity": 3},
        {"product_id": new["id"], "quantity": 1},
    ]}
    updated = client.put(path, headers=user["headers"], json=mixed)
    assert updated.status_code == 200, updated.text
    assert updated.json()["total_amount"] == 550
    assert {item["product_id"]: item["price_at_order"] for item in updated.json()["items"]} == {
        p["id"]: 100, new["id"]: 250,
    }
    removed = client.put(path, headers=user["headers"], json={"items": [mixed["items"][1]]})
    assert removed.status_code == 200, removed.text
    mixed["items"][0]["quantity"] = 1
    readded = client.put(path, headers=user["headers"], json=mixed)
    assert readded.status_code == 200, readded.text
    assert readded.json()["total_amount"] == 400
    assert {item["product_id"]: item["price_at_order"] for item in readded.json()["items"]} == {
        p["id"]: 150, new["id"]: 250,
    }


@pytest.mark.parametrize("mode", ["expired", "future", "inactive"])
def test_invalid_promos_rollback_reservations(client, account, product, sql, mode):
    now = datetime.now(UTC)
    overrides = (
        {"active": False}
        if mode == "inactive"
        else {
            "valid_from": (now + timedelta(days=1 if mode == "future" else -2)).isoformat(),
            "valid_until": (now + timedelta(days=2 if mode == "future" else -1)).isoformat(),
        }
    )
    p = product()
    code = promo(client, account, **overrides)
    result = place(client, account(), p, promo_code=code["code"])
    assert result.status_code == 422 and result.json()["error_code"] == "PROMO_CODE_INVALID"
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 10
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 0
    assert sql("SELECT count(*) AS n FROM orders")[0]["n"] == 0


def test_concurrent_last_promo_use_is_not_overspent(client, account, product, sql):
    p = product()
    code = promo(client, account, max_uses=1)
    users = [account(), account()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda user: place(client, user, p, promo_code=code["code"]), users))
    assert sorted(result.status_code for result in results) == [201, 422]
    assert sql("SELECT current_uses FROM promo_codes")[0]["current_uses"] == 1
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 8


def test_order_state_machine(client, account, product):
    user, admin = account(), account("ADMIN")
    p = product()
    order = place(client, user, p).json()
    path = "/orders/" + order["id"]
    assert client.post(path + "/status", headers=user["headers"], json={"status": "PAID"}).status_code == 403
    assert client.post(path + "/status", headers=admin["headers"], json={"status": "PAID"}).status_code == 409
    for status in ["PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"]:
        assert (
            client.post(path + "/status", headers=admin["headers"], json={"status": status}).status_code
            == 200
        )
        assert (
            client.put(
                path, headers=user["headers"], json={"items": [{"product_id": p["id"], "quantity": 1}]}
            ).json()["error_code"]
            == "INVALID_STATE_TRANSITION"
        )
    assert client.post(path + "/cancel", headers=user["headers"]).status_code == 409


def test_concurrent_reservations_never_oversell(client, account, product, sql):
    p = product(stock=3)
    users = [account(), account()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda u: place(client, u, p, 2), users))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 1
    assert sql("SELECT count(*) AS n FROM orders")[0]["n"] == 1


def test_concurrent_same_user_has_one_active_order(client, account, product, sql):
    p, user = product(), account()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: place(client, user, p, 1), range(2)))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert sql("SELECT count(*) AS n FROM orders")[0]["n"] == 1


def test_product_restock_keeps_capacity_for_reserved_returns(client, account, product, sql):
    p, buyer, admin = product(stock=1), account(), account("ADMIN")
    order = place(client, buyer, p, 1).json()
    payload = dict(name="Book", price=100, stock=2147483647, category="books", status="ACTIVE")
    result = client.put("/products/" + p["id"], headers=admin["headers"], json=payload)
    assert result.status_code == 400, result.text
    assert result.json()["error_code"] == "VALIDATION_ERROR"
    assert client.post("/orders/" + order["id"] + "/cancel", headers=buyer["headers"]).status_code == 200
    assert sql("SELECT stock FROM products WHERE id=%s", (p["id"],))[0]["stock"] == 1


@pytest.mark.parametrize(
    "items,promo_code",
    [
        ([], None),
        ([{"product_id": str(uuid4()), "quantity": 0}], None),
        ([{"product_id": str(uuid4()), "quantity": 1000}], None),
        ([{"product_id": str(uuid4()), "quantity": 1}] * 51, None),
        ([{"product_id": str(uuid4()), "quantity": 1}], "bad!"),
    ],
)
def test_order_contract_validation(client, account, sql, items, promo_code):
    response = client.post(
        "/orders", headers=account()["headers"], json={"items": items, "promo_code": promo_code}
    )
    assert response.status_code == 400
    assert response.json()["details"]["fields"]
    assert sql("SELECT count(*) AS n FROM user_operations")[0]["n"] == 0
