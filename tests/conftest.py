import json
import os
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_schema_validator import OAS30Validator
from psycopg.rows import dict_row


@pytest.fixture
def db_url(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database migrated with Flyway")
    # Destructive test cleanup is restricted to an explicitly named test database.
    from psycopg.conninfo import conninfo_to_dict

    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL database name must end with _test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-at-least-thirty-two-characters")
    monkeypatch.setenv("ORDER_RATE_LIMIT_MINUTES", "0")
    with psycopg.connect(url) as conn:
        assert conn.execute(
            "SELECT success FROM flyway_schema_history ORDER BY installed_rank DESC LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "TRUNCATE refresh_tokens, user_operations, order_items, orders, "
            "promo_codes, products, users CASCADE"
        )
    return url


@pytest.fixture
def client(db_url):
    from app.main import app

    source = yaml.safe_load(Path("src/main/resources/openapi/marketplace.yaml").read_text(encoding="utf-8"))
    contract = json.loads(json.dumps(source), parse_float=Decimal)

    def dereference(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = contract
                for key in node["$ref"][2:].split("/"):
                    target = target[key]
                return dereference(target)
            return {key: dereference(value) for key, value in node.items()}
        if isinstance(node, list):
            return [dereference(value) for value in node]
        return node

    class ContractClient(TestClient):
        def request(self, method, url, **kwargs):
            result = super().request(method, url, **kwargs)
            path = urlsplit(str(result.request.url)).path
            for template, operations in contract["paths"].items():
                if not re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path):
                    continue
                operation = operations.get(method.lower())
                if not operation or not result.content:
                    break
                responses = operation["responses"]
                definition = dereference(responses.get(str(result.status_code), responses.get("default")))
                schema = definition.get("content", {}).get("application/json", {}).get("schema")
                if schema:
                    payload = json.loads(result.content, parse_float=Decimal)
                    OAS30Validator(schema).validate(payload)
                break
            return result

    with ContractClient(app) as client:
        yield client


@pytest.fixture
def sql(db_url):
    def run(query, params=()):
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    return run


@pytest.fixture
def account(client, sql):
    def make(role="USER"):
        username = "user_" + uuid4().hex[:12]
        response = client.post(
            "/auth/register",
            json={
                "username": username,
                "password": "password123",
                "role": "USER" if role == "ADMIN" else role,
            },
        )
        assert response.status_code == 201, response.text
        user = response.json()
        if role == "ADMIN":
            sql("UPDATE users SET role = %s WHERE id = %s", ("ADMIN", user["id"]))
        response = client.post("/auth/login", json={"username": username, "password": "password123"})
        assert response.status_code == 200, response.text
        tokens = response.json()
        return {
            "id": user["id"],
            "headers": {"Authorization": "Bearer " + tokens["access_token"]},
            "tokens": tokens,
            "username": username,
        }

    return make


@pytest.fixture
def product(client, account):
    seller = account("SELLER")

    def make(**overrides):
        payload = dict(name="Book", description=None, price=100, stock=10, category="books", status="ACTIVE")
        response = client.post("/products", headers=seller["headers"], json=payload | overrides)
        assert response.status_code == 201, response.text
        return response.json()

    return make
