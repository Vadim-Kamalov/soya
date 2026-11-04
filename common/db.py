"""Versioned transactional SQL migrations, serialized across service replicas."""
from pathlib import Path
import hashlib
import psycopg
from psycopg.rows import dict_row


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


def migrate(dsn, service):
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(178231)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        for path in sorted((Path(__file__).resolve().parents[1] / "migrations" / service).glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            applied = conn.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (path.name,)).fetchone()
            if applied:
                if applied["checksum"] != checksum:
                    raise RuntimeError(f"Applied migration modified: {path.name}")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)", (path.name, checksum))

