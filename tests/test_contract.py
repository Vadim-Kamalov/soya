from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

SPEC = Path("src/main/resources/openapi/marketplace.yaml")


def test_contract_valid_and_covers_all_operations():
    assert SPEC.exists(), "Contract-first OpenAPI specification is missing"
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    validate(spec)
    required = {
        "/products": {"get", "post"},
        "/products/{id}": {"get", "put", "delete"},
        "/orders": {"post"},
        "/orders/{id}": {"get", "put"},
        "/orders/{id}/cancel": {"post"},
        "/promo-codes": {"post"},
        "/auth/register": {"post"},
        "/auth/login": {"post"},
        "/auth/refresh": {"post"},
    }
    for path, methods in required.items():
        assert methods <= spec["paths"][path].keys()
    schemas = spec["components"]["schemas"]
    assert schemas["ProductCreate"]["properties"]["price"]["minimum"] == 0.01
    assert schemas["OrderCreate"]["properties"]["items"]["maxItems"] == 50


def test_generated_models_enforce_contract():
    from pydantic import ValidationError

    from app.generated.models import ProductCreate

    valid = dict(name="Book", price=10.01, stock=2, category="books", status="ACTIVE")
    assert str(ProductCreate(**valid).price) == "10.01"
    for field, value in [("name", ""), ("price", 0), ("stock", -1), ("category", "")]:
        with pytest.raises(ValidationError):
            ProductCreate(**(valid | {field: value}))


def test_generated_decimal_bounds_and_json_types():
    from pydantic import ValidationError

    from app.generated.models import ProductCreate

    payload = dict(name="Book", price=0.01, stock=0, category="books", status="ACTIVE")
    assert str(ProductCreate(**payload).price) == "0.01"
    assert str(ProductCreate(**(payload | {"price": 9999999999.99})).price) == "9999999999.99"
    for field, value in [("price", True), ("price", "10.00"), ("stock", "2"), ("stock", True)]:
        with pytest.raises(ValidationError):
            ProductCreate(**(payload | {field: value}))


def test_datetime_contract_rejects_epoch_numbers_and_naive_dates():
    from pydantic import ValidationError

    from app.generated.models import PromoCreate

    payload = dict(
        code="TEST",
        discount_type="PERCENTAGE",
        discount_value=10,
        min_order_amount=0,
        max_uses=1,
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2027-01-01T00:00:00Z",
    )
    assert PromoCreate(**payload).valid_from.tzinfo is not None
    for value in [1700000000, "1700000000", "2026-01-01T00:00:00", "2026-01-01"]:
        with pytest.raises(ValidationError):
            PromoCreate(**(payload | {"valid_from": value}))
