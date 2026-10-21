"""Run real HTTP E2E against a running marketplace and optionally inspect its DB."""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--database-url", default=os.getenv("DEMO_DATABASE_URL"))
    args = parser.parse_args()
    suffix = uuid4().hex[:8]
    with httpx.Client(base_url=args.url, timeout=20) as client:

        def call(method, path, expected=200, headers=None, **kwargs):
            result = client.request(method, path, headers=headers, **kwargs)
            assert result.status_code == expected, f"{method} {path}: {result.status_code} {result.text}"
            print(f"{method} {path} -> {result.status_code}, request_id={result.headers.get('X-Request-Id')}")
            return result.json() if result.content else None

        def account(role):
            credentials = {
                "username": f"{role.lower()}_{suffix}_{uuid4().hex[:4]}",
                "password": "demo-password123",
            }
            call("POST", "/auth/register", 201, json=credentials | {"role": role})
            pair = call("POST", "/auth/login", json=credentials)
            return {"Authorization": "Bearer " + pair["access_token"]}, pair

        seller, _ = account("SELLER")
        buyer, pair = account("USER")
        other, _ = account("USER")
        p = call(
            "POST",
            "/products",
            201,
            seller,
            json={
                "name": f"Demo book {suffix}",
                "price": 100,
                "stock": 10,
                "category": "books",
                "status": "ACTIVE",
            },
        )
        pid = p["id"]
        now = datetime.now(UTC)
        promo = call(
            "POST",
            "/promo-codes",
            201,
            seller,
            json={
                "code": "DEMO_" + suffix.upper(),
                "discount_type": "PERCENTAGE",
                "discount_value": 10,
                "min_order_amount": 150,
                "max_uses": 2,
                "valid_from": (now - timedelta(days=1)).isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
            },
        )
        order = call(
            "POST",
            "/orders",
            201,
            buyer,
            json={
                "items": [{"product_id": pid, "quantity": 2}],
                "promo_code": promo["code"],
            },
        )
        assert order["total_amount"] == 180 and order["discount_amount"] == 20
        print(f"Order: {order['id']}, total=180, discount=20")
        path = "/orders/" + order["id"]
        denied = call("GET", path, 403, other)
        assert denied["error_code"] == "ORDER_OWNERSHIP_VIOLATION"
        failed = call("PUT", path, 409, buyer, json={"items": [{"product_id": pid, "quantity": 11}]})
        assert failed["error_code"] == "INSUFFICIENT_STOCK"
        assert call("GET", "/products/" + pid, headers=buyer)["stock"] == 8
        updated = call("PUT", path, headers=buyer, json={"items": [{"product_id": pid, "quantity": 1}]})
        assert updated["total_amount"] == 100 and updated["promo_code_id"] is None
        canceled = call("POST", path + "/cancel", headers=buyer)
        assert canceled["status"] == "CANCELED"
        assert call("GET", "/products/" + pid, headers=buyer)["stock"] == 10
        assert call("POST", path + "/cancel", 409, buyer)["error_code"] == "INVALID_STATE_TRANSITION"
        assert (
            call(
                "POST",
                "/products",
                403,
                buyer,
                json={
                    "name": "denied",
                    "price": 1,
                    "stock": 1,
                    "category": "x",
                    "status": "ACTIVE",
                },
            )["error_code"]
            == "ACCESS_DENIED"
        )
        invalid = call("POST", "/orders", 400, buyer, json={"items": []})
        assert invalid["error_code"] == "VALIDATION_ERROR"
        call("DELETE", "/products/" + pid, 204, seller)
        page = call("GET", "/products?status=ARCHIVED&category=books&size=100", headers=buyer)
        assert any(item["id"] == pid for item in page["items"]) or page["totalElements"] > 100
        new_pair = call("POST", "/auth/refresh", json={"refresh_token": pair["refresh_token"]})
        assert new_pair["access_token"] != pair["access_token"]
        call("POST", "/auth/refresh", 401, json={"refresh_token": pair["refresh_token"]})
        print(f"PRODUCT_ID={pid}\nORDER_ID={order['id']}")
    if args.database_url:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(args.database_url, row_factory=dict_row) as conn:
            for table, query in {
                "products": "SELECT id,name,stock,status,seller_id FROM products WHERE id=%s",
                "orders": "SELECT * FROM orders WHERE id=%s",
                "order_items": "SELECT * FROM order_items WHERE order_id=%s",
                "promo_codes": "SELECT code,current_uses FROM promo_codes WHERE id=%s",
                "user_operations": "SELECT user_id,operation_type,created_at FROM user_operations "
                "WHERE user_id=(SELECT user_id FROM orders WHERE id=%s)",
            }.items():
                param = pid if table == "products" else promo["id"] if table == "promo_codes" else order["id"]
                rows = conn.execute(query, (param,)).fetchall()
                print(table, json.dumps(rows, default=str, ensure_ascii=False))
    print("E2E passed")


if __name__ == "__main__":
    main()
