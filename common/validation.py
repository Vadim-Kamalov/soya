from decimal import Decimal, InvalidOperation
from uuid import UUID
import re
import grpc
from common.errors import fail


def uuid_string(value, name="id"):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        fail(grpc.StatusCode.INVALID_ARGUMENT, f"{name} must be a UUID")


def iata(value):
    if not re.fullmatch(r"[A-Z]{3}", value):
        fail(grpc.StatusCode.INVALID_ARGUMENT, "Airport must be a three-letter uppercase IATA code")
    return value


def money(value):
    try:
        result = Decimal(value)
        if not result.is_finite() or result <= 0 or result > Decimal("9999999999.99"):
            raise ValueError()
        if result != result.quantize(Decimal("0.01")):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        fail(grpc.StatusCode.INVALID_ARGUMENT, "Price must be positive, with at most two decimal places")

