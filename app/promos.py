from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from psycopg.errors import UniqueViolation

from app.auth import require_role
from app.db import transaction
from app.errors import ApiError, invalid

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def create(body, user):
    require_role(user, "SELLER", "ADMIN")
    if body.valid_from.tzinfo is None or body.valid_until.tzinfo is None:
        invalid("valid_from/valid_until", "Timezone offset is required")
    if body.valid_until <= body.valid_from:
        invalid("valid_until", "Must be later than valid_from")
    try:
        with transaction() as conn:
            return conn.execute(
                "INSERT INTO promo_codes(code,discount_type,discount_value,min_order_amount,max_uses,"
                "valid_from,valid_until,active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    body.code,
                    body.discount_type.value,
                    body.discount_value,
                    body.min_order_amount,
                    body.max_uses,
                    body.valid_from,
                    body.valid_until,
                    body.active,
                ),
            ).fetchone()
    except UniqueViolation:
        raise ApiError("PROMO_ALREADY_EXISTS", 409, "Promo code already exists") from None


def applicable(promo, subtotal, own_use=False):
    now = datetime.now(UTC)
    if (
        not promo
        or not promo["active"]
        or not promo["valid_from"] <= now <= promo["valid_until"]
        or promo["current_uses"] - int(own_use) >= promo["max_uses"]
    ):
        return "PROMO_CODE_INVALID"
    if subtotal < promo["min_order_amount"]:
        return "PROMO_CODE_MIN_AMOUNT"
    return None


def discount(promo, subtotal):
    if promo["discount_type"] == "PERCENTAGE":
        if promo["discount_value"] > Decimal("70"):
            return ZERO
        amount = subtotal * promo["discount_value"] / 100
        rounded = amount.quantize(CENT, rounding=ROUND_HALF_UP)
        ceiling = (subtotal * Decimal("0.70")).quantize(CENT, rounding=ROUND_DOWN)
        return min(rounded, ceiling)
    else:
        amount = min(promo["discount_value"], subtotal)
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def apply_new(conn, code, subtotal):
    if not code:
        return None, ZERO
    promo = conn.execute("SELECT * FROM promo_codes WHERE code=%s FOR UPDATE", (code,)).fetchone()
    error = applicable(promo, subtotal)
    if error:
        raise ApiError(error, 422, "Promo code is invalid or order does not meet minimum amount")
    conn.execute("UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s", (promo["id"],))
    return promo["id"], discount(promo, subtotal)


def reapply(conn, promo_id, subtotal):
    if promo_id is None:
        return None, ZERO
    promo = conn.execute("SELECT * FROM promo_codes WHERE id=%s FOR UPDATE", (promo_id,)).fetchone()
    if applicable(promo, subtotal, own_use=True):
        release(conn, promo_id)
        return None, ZERO
    return promo_id, discount(promo, subtotal)


def release(conn, promo_id):
    if promo_id is not None:
        conn.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (promo_id,))
