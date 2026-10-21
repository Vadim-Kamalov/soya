from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import settings


@contextmanager
def transaction():
    # A fresh connection per operation keeps transaction boundaries explicit.
    # psycopg commits on normal exit and rolls back on any exception.
    with psycopg.connect(settings().database_url, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("SET LOCAL TIME ZONE 'UTC'")
        conn.execute("SET LOCAL lock_timeout = '10s'")
        yield conn
