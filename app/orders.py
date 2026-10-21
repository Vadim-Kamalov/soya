from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app import promos
from app.auth import require_role
from app.config import settings
from app.db import transaction
from app.errors import ApiError, invalid

NEXT_STATUS = {
    "CREATED": "PAYMENT_PENDING",
    "PAYMENT_PENDING": "PAID",
    "PAID": "SHIPPED",
    "SHIPPED": "COMPLETED",
}


def aggregate(items):
    quantities = Counter()
    for item in items:
        quantities[item.product_id] += item.quantity
    return quantities


def lock_products(conn, ids):
    rows = conn.execute(
        "SELECT * FROM products WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (list(ids),)
    ).fetchall()
    return {row["id"]: row for row in rows}


def check_catalog(products, quantities):
    for product_id in quantities:
        if product_id not in products:
            raise ApiError(
                "PRODUCT_NOT_FOUND", 404, "Product does not exist", {"product_id": str(product_id)}
            )
        if products[product_id]["status"] != "ACTIVE":
            raise ApiError("PRODUCT_INACTIVE", 409, "Product is not active", {"product_id": str(product_id)})


def reserve(conn, products, quantities):
    shortages = [
        {"product_id": str(pid), "requested": qty, "available": products[pid]["stock"]}
        for pid, qty in quantities.items()
        if products[pid]["stock"] < qty
    ]
    if shortages:
        raise ApiError("INSUFFICIENT_STOCK", 409, "Insufficient stock", {"products": shortages})
    for pid, qty in quantities.items():
        conn.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (qty, pid))


def subtotal_for(prices, quantities):
    return sum((prices[pid] * qty for pid, qty in quantities.items()), Decimal("0.00"))


def final_total(subtotal, discount):
    total = subtotal - discount
    if total > Decimal("9999999999.99") or discount > Decimal("9999999999.99"):
        invalid("items", "Order total_amount or discount_amount exceeds DECIMAL(12,2) capacity")
    return total


def check_rate(conn, user_id, operation):
    # Lock the user even if no operation row exists yet (first-operation race).
    conn.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
    latest = conn.execute(
        "SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, operation),
    ).fetchone()
    minutes = settings().order_rate_limit_minutes
    if latest and datetime.now(UTC) - latest["created_at"] < timedelta(minutes=float(minutes)):
        raise ApiError(
            "ORDER_LIMIT_EXCEEDED",
            429,
            "Order operation rate limit exceeded",
            {"operation_type": operation, "limit_minutes": float(minutes)},
        )


def record(conn, user_id, operation):
    conn.execute("INSERT INTO user_operations(user_id,operation_type) VALUES (%s,%s)", (user_id, operation))


def save_items(conn, order_id, prices, quantities):
    for pid, qty in quantities.items():
        conn.execute(
            "INSERT INTO order_items(order_id,product_id,quantity,price_at_order) VALUES (%s,%s,%s,%s)",
            (order_id, pid, qty, prices[pid]),
        )


def fetch(conn, order_id, user, lock=False):
    order = conn.execute(
        "SELECT * FROM orders WHERE id=%s" + (" FOR UPDATE" if lock else ""), (order_id,)
    ).fetchone()
    if not order:
        raise ApiError("ORDER_NOT_FOUND", 404, "Order does not exist")
    if user["role"] != "ADMIN" and order["user_id"] != user["id"]:
        raise ApiError("ORDER_OWNERSHIP_VIOLATION", 403, "Order belongs to another user")
    return order


def with_items(conn, order):
    return order | {
        "items": conn.execute(
            "SELECT id,product_id,quantity,price_at_order FROM order_items "
            "WHERE order_id=%s ORDER BY product_id",
            (order["id"],),
        ).fetchall()
    }


def get(order_id, user):
    require_role(user, "USER", "ADMIN")
    with transaction() as conn:
        # The order and its item collection must belong to the same snapshot.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return with_items(conn, fetch(conn, order_id, user))


def create(body, user):
    require_role(user, "USER", "ADMIN")
    quantities = aggregate(body.items)
    with transaction() as conn:
        check_rate(conn, user["id"], "CREATE_ORDER")
        active = conn.execute(
            "SELECT id FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')",
            (user["id"],),
        ).fetchone()
        if active:
            raise ApiError("ORDER_HAS_ACTIVE", 409, "User already has an active order")
        products = lock_products(conn, quantities)
        check_catalog(products, quantities)
        reserve(conn, products, quantities)
        prices = {pid: product["price"] for pid, product in products.items()}
        subtotal = subtotal_for(prices, quantities)
        promo_id, discount = promos.apply_new(conn, body.promo_code, subtotal)
        total = final_total(subtotal, discount)
        order = conn.execute(
            "INSERT INTO orders(user_id,status,promo_code_id,total_amount,discount_amount) "
            "VALUES (%s,'CREATED',%s,%s,%s) RETURNING *",
            (user["id"], promo_id, total, discount),
        ).fetchone()
        save_items(conn, order["id"], prices, quantities)
        record(conn, user["id"], "CREATE_ORDER")
        return with_items(conn, order)


def update(order_id, body, user):
    require_role(user, "USER", "ADMIN")
    quantities = aggregate(body.items)
    with transaction() as conn:
        order = fetch(conn, order_id, user, lock=True)
        if order["status"] != "CREATED":
            raise ApiError("INVALID_STATE_TRANSITION", 409, "Only CREATED orders can be updated")
        check_rate(conn, user["id"], "UPDATE_ORDER")
        old_items = conn.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,)).fetchall()
        products = lock_products(conn, set(quantities) | {item["product_id"] for item in old_items})
        prices = {pid: product["price"] for pid, product in products.items()}
        for item in old_items:
            pid, qty = item["product_id"], item["quantity"]
            conn.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (qty, pid))
            products[pid]["stock"] += qty
            # Retained products keep their original price, including added units.
            prices[pid] = item["price_at_order"]
        check_catalog(products, quantities)
        reserve(conn, products, quantities)
        subtotal = subtotal_for(prices, quantities)
        promo_id, discount = promos.reapply(conn, order["promo_code_id"], subtotal)
        total = final_total(subtotal, discount)
        conn.execute("DELETE FROM order_items WHERE order_id=%s", (order_id,))
        save_items(conn, order_id, prices, quantities)
        order = conn.execute(
            "UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s RETURNING *",
            (total, discount, promo_id, order_id),
        ).fetchone()
        record(conn, user["id"], "UPDATE_ORDER")
        return with_items(conn, order)


def cancel(order_id, user):
    require_role(user, "USER", "ADMIN")
    with transaction() as conn:
        order = fetch(conn, order_id, user, lock=True)
        if order["status"] not in {"CREATED", "PAYMENT_PENDING"}:
            raise ApiError("INVALID_STATE_TRANSITION", 409, "Order cannot be canceled in this state")
        items = conn.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,)).fetchall()
        lock_products(conn, {item["product_id"] for item in items})
        for item in items:
            conn.execute(
                "UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"], item["product_id"])
            )
        promos.release(conn, order["promo_code_id"])
        order = conn.execute(
            "UPDATE orders SET status='CANCELED' WHERE id=%s RETURNING *", (order_id,)
        ).fetchone()
        return with_items(conn, order)


def transition(order_id, body, user):
    require_role(user, "ADMIN")
    with transaction() as conn:
        order = fetch(conn, order_id, user, lock=True)
        if NEXT_STATUS.get(order["status"]) != body.status.value:
            raise ApiError("INVALID_STATE_TRANSITION", 409, "Order state transition is not allowed")
        order = conn.execute(
            "UPDATE orders SET status=%s WHERE id=%s RETURNING *", (body.status.value, order_id)
        ).fetchone()
        return with_items(conn, order)
