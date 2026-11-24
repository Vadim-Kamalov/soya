import json
import logging
import os
from time import sleep

import psycopg
import requests

log = logging.getLogger(__name__)
CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "http://clickhouse:8123")
PG_DSN = os.getenv("POSTGRES_DSN", "postgresql://cinema:cinema@postgres:5432/cinema")


class ClickHouse:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (os.getenv("CLICKHOUSE_USER", "analytics"), os.getenv("CLICKHOUSE_PASSWORD", "analytics"))

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.session.close()

    def execute(self, sql, data=None, **parameters):
        params = {f"param_{name}": value for name, value in parameters.items()}
        params.update(wait_end_of_query=1, output_format_json_quote_64bit_integers=0)
        if data is None:
            response = self.session.post(CLICKHOUSE_URL, data=sql.encode(), params=params, timeout=60)
        else:
            response = self.session.post(CLICKHOUSE_URL, data=data.encode(), params={**params, "query": sql}, timeout=60)
        if not response.ok:
            raise RuntimeError(f"ClickHouse HTTP {response.status_code}: {response.text.strip()}")
        return response.text

    def rows(self, sql, **parameters):
        result = self.execute(f"{sql.rstrip()}\nFORMAT JSONEachRow", **parameters)
        return [json.loads(line) for line in result.splitlines()]


def pg_connect():
    return psycopg.connect(PG_DSN, connect_timeout=5, options="-c statement_timeout=120000")


def lock_day(connection, day):
    connection.execute("SELECT pg_advisory_xact_lock(5205, %s)", (day.toordinal(),))


def retry(operation, errors, *, attempts=4):
    for attempt in range(attempts):
        try:
            return operation()
        except errors:
            if attempt == attempts - 1:
                raise
            delay = min(8, 2**attempt)
            log.warning("%s failed, retrying in %ss", operation.__name__, delay, exc_info=True)
            sleep(delay)
