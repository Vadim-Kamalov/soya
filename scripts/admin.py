"""Explicit provisioning of an administrator; public registration cannot grant ADMIN."""

import argparse
import getpass
import os

from psycopg.errors import UniqueViolation

from app.auth import password_hash
from app.db import transaction
from app.generated.models import LoginRequest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    args = parser.parse_args()
    password = os.getenv("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    data = LoginRequest(username=args.username, password=password)
    try:
        with transaction() as conn:
            row = conn.execute(
                "INSERT INTO users(username,password_hash,role) VALUES (%s,%s,'ADMIN') "
                "RETURNING id,username,role",
                (data.username, password_hash.hash(data.password.get_secret_value())),
            ).fetchone()
        print(f"Created {row['role']} {row['username']}: {row['id']}")
    except UniqueViolation:
        parser.exit(1, "Username already exists; no changes made.\n")


if __name__ == "__main__":
    main()
