from app.auth import require_role
from app.db import transaction
from app.errors import ApiError, invalid


def find(conn, product_id, lock=False):
    row = conn.execute(
        "SELECT * FROM products WHERE id=%s" + (" FOR UPDATE" if lock else ""), (product_id,)
    ).fetchone()
    if not row:
        raise ApiError("PRODUCT_NOT_FOUND", 404, "Product does not exist", {"product_id": str(product_id)})
    return row


def can_manage(user, product):
    require_role(user, "SELLER", "ADMIN")
    if user["role"] != "ADMIN" and product["seller_id"] != user["id"]:
        raise ApiError("ACCESS_DENIED", 403, "Seller can manage only own products")


def create(body, user):
    require_role(user, "SELLER", "ADMIN")
    seller_id = body.seller_id or user["id"]
    if user["role"] != "ADMIN" and seller_id != user["id"]:
        raise ApiError("ACCESS_DENIED", 403, "Seller can create only own products")
    with transaction() as conn:
        seller = conn.execute("SELECT role FROM users WHERE id=%s", (seller_id,)).fetchone()
        if not seller or seller["role"] not in {"SELLER", "ADMIN"}:
            invalid("seller_id", "Must reference an existing SELLER or ADMIN")
        return conn.execute(
            "INSERT INTO products(name,description,price,stock,category,status,seller_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                body.name,
                body.description,
                body.price,
                body.stock,
                body.category,
                body.status.value,
                seller_id,
            ),
        ).fetchone()


def get(product_id):
    with transaction() as conn:
        return find(conn, product_id)


def list_all(page, size, status, category):
    predicates, params = [], []
    if status is not None:
        predicates.append("status=%s")
        params.append(status.value)
    if category is not None:
        predicates.append("category=%s")
        params.append(category)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    with transaction() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        count = conn.execute("SELECT count(*) AS n FROM products" + where, params).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM products" + where + " ORDER BY created_at,id LIMIT %s OFFSET %s",
            params + [size, page * size],
        ).fetchall()
        return {"items": rows, "totalElements": count, "page": page, "size": size}


def update(product_id, body, user):
    require_role(user, "SELLER", "ADMIN")
    with transaction() as conn:
        can_manage(user, find(conn, product_id, lock=True))
        reserved = conn.execute(
            "SELECT coalesce(sum(i.quantity),0) AS n FROM order_items i JOIN orders o ON o.id=i.order_id "
            "WHERE i.product_id=%s AND o.status IN ('CREATED','PAYMENT_PENDING')",
            (product_id,),
        ).fetchone()["n"]
        if body.stock + reserved > 2147483647:
            invalid("stock", "Available stock plus cancelable reservations exceeds warehouse capacity")
        return conn.execute(
            "UPDATE products SET name=%s,description=%s,price=%s,stock=%s,category=%s,status=%s "
            "WHERE id=%s RETURNING *",
            (
                body.name,
                body.description,
                body.price,
                body.stock,
                body.category,
                body.status.value,
                product_id,
            ),
        ).fetchone()


def archive(product_id, user):
    require_role(user, "SELLER", "ADMIN")
    with transaction() as conn:
        can_manage(user, find(conn, product_id, lock=True))
        conn.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s", (product_id,))
