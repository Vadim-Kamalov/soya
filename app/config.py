import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    order_rate_limit_minutes: Decimal
    access_minutes: int = 20
    refresh_days: int = 14


def settings():
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Set JWT_SECRET to at least 32 characters")
    rate = Decimal(os.getenv("ORDER_RATE_LIMIT_MINUTES", "1"))
    if not rate.is_finite() or rate < 0:
        raise RuntimeError("ORDER_RATE_LIMIT_MINUTES must be finite and nonnegative")
    return Settings(os.environ["DATABASE_URL"], secret, rate)
