import json
import logging
import os
from contextlib import closing
from datetime import date
from pathlib import Path
from threading import Lock

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from psycopg.rows import dict_row

from .settings import ROOT
from .storage import lock_day, pg_connect

log = logging.getLogger(__name__)
BUCKET = "movie-analytics"
EXPORT_REQUESTS_DIR = Path(os.getenv("EXPORT_REQUESTS_DIR", ROOT / ".export-requests"))
export_lock = Lock()


class NoMetricsError(LookupError):
    pass


def s3_client():
    return boto3.client(
        "s3", endpoint_url=os.getenv("S3_ENDPOINT", "http://s3:8333"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}, s3={"addressing_style": "path"}),
    )


def ensure_bucket(client):
    try:
        client.head_bucket(Bucket=BUCKET)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        try:
            client.create_bucket(Bucket=BUCKET)
        except ClientError as create_error:
            if create_error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise


def pending_days(today):
    with pg_connect() as connection:
        scheduled = {day for (day,) in connection.execute("""
            SELECT metrics.day
            FROM daily_metrics AS metrics LEFT JOIN daily_exports AS exports USING (day)
            WHERE metrics.day < %s
            GROUP BY metrics.day, exports.computed_at
            HAVING max(metrics.computed_at) IS DISTINCT FROM exports.computed_at
            """, (today,))}
    requested = {date.fromisoformat(path.name) for path in EXPORT_REQUESTS_DIR.glob("????-??-??")}
    return sorted(scheduled | requested)


def export_day(day, *, requested=False):
    marker = EXPORT_REQUESTS_DIR / day.isoformat()
    with export_lock:
        if requested:
            EXPORT_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
            marker.touch()
        try:
            result = _export_snapshot(day)
        except NoMetricsError:
            marker.unlink(missing_ok=True)
            raise
        marker.unlink(missing_ok=True)
        return result


def _export_snapshot(day):
    key = f"daily/{day}/aggregates.json"
    with pg_connect() as connection:
        lock_day(connection, day)
        with connection.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute("""
                SELECT metric, movie_id, value, computed_at AT TIME ZONE 'UTC' AS computed_at
                FROM daily_metrics WHERE day = %s ORDER BY metric, movie_id
                """, (day,)).fetchall()
        if not rows:
            raise NoMetricsError(f"No metrics for {day}")
        version = connection.execute("SELECT max(computed_at) FROM daily_metrics WHERE day = %s", (day,)).fetchone()[0]
        for row in rows:
            row["computed_at"] = row["computed_at"].isoformat() + "Z"
        payload = {"date": day.isoformat(), "metrics": rows}
        with closing(s3_client()) as client:
            ensure_bucket(client)
            client.put_object(Bucket=BUCKET, Key=key, ContentType="application/json", Body=json.dumps(payload, allow_nan=False).encode())
        connection.execute("""
            INSERT INTO daily_exports (day, computed_at) VALUES (%s, %s)
            ON CONFLICT (day) DO UPDATE SET computed_at = EXCLUDED.computed_at, exported_at = now()
            """, (day, version))
    log.info("Exported %s metrics to s3://%s/%s", len(rows), BUCKET, key)
    return {"bucket": BUCKET, "key": key, "metrics_exported": len(rows)}
