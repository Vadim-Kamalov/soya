import json
import logging
from datetime import UTC, date, datetime, timedelta
from time import monotonic

import psycopg

from .settings import MAX_MOVIE_ID_BYTES, RETENTION_DAYS, utc_today
from .storage import ClickHouse, lock_day, pg_connect, retry

log = logging.getLogger(__name__)

DAILY_SQL = """
SELECT count() AS events_processed,
       uniqExact(user_id) AS dau,
       avgOrNullIf(progress_seconds, event_type = 'VIEW_FINISHED') AS avg_watch_seconds,
       countIf(event_type = 'VIEW_FINISHED') / nullIf(countIf(event_type = 'VIEW_STARTED'), 0) AS view_conversion,
       sumMapIf(map(movie_id, toUInt64(1)), event_type = 'VIEW_STARTED'
           AND position(movie_id, char(0)) = 0 AND length(movie_id) <= {max_movie_id_bytes:UInt32}) AS views_by_movie,
       arraySlice(arraySort(movie -> (-toInt64(movie.2), movie.1),
           arrayZip(mapKeys(views_by_movie), mapValues(views_by_movie))), 1, 10) AS top_movies
FROM cinema.raw_events FINAL
WHERE event_date = {day:Date32}
"""

COHORT_SQL = """
WITH {day:Date32} AS target_day
SELECT countIf(first_view = target_day) AS cohort_size,
       countIf(first_activity = target_day) AS activity_cohort_size,
       sumForEach(arrayMap(day_offset -> toUInt64(ifNull(first_view = target_day, 0)
           AND has(activity_days, addDays(target_day, day_offset))), range({retention_window:UInt32}))) AS retained,
       sumForEach(arrayMap(day_offset -> toUInt64(first_activity = target_day
           AND has(activity_days, addDays(target_day, day_offset))), range({retention_window:UInt32}))) AS activity_retained
FROM (
    SELECT user_id, minOrNullIf(event_date, event_type = 'VIEW_STARTED') AS first_view,
           min(event_date) AS first_activity,
           groupUniqArrayIf(event_date, event_date >= target_day) AS activity_days
    FROM cinema.raw_events FINAL
    WHERE event_date < addDays(target_day, {retention_window:UInt32})
    GROUP BY user_id
    HAVING first_view = target_day OR first_activity = target_day
)
"""

UPSERT_SQL = """
INSERT INTO daily_metrics (day, metric, movie_id, value, computed_at)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (day, metric, movie_id) DO UPDATE
SET value = EXCLUDED.value, computed_at = EXCLUDED.computed_at
"""


def unprocessed_days(before):
    with ClickHouse() as ch, pg_connect() as connection:
        dates = ch.rows("""
            SELECT DISTINCT event_date AS day FROM cinema.raw_events
            WHERE event_date < {before:Date32}
            """, before=before.isoformat())
        processed = {day.isoformat() for (day,) in connection.execute("""
            SELECT day FROM daily_metrics
            WHERE day < %s AND metric = 'activity_cohort_size'
            """, (before,))}
    return sorted(date.fromisoformat(row["day"]) for row in dates if row["day"] not in processed)


def calculate(day, *, force=True):
    start = monotonic()
    log.info("Aggregation started day=%s", day)

    def calculate_once():
        with pg_connect() as connection, ClickHouse() as ch:
            lock_day(connection, day)
            return aggregate_day(connection, ch, day, force)

    try:
        count, changed = retry(calculate_once, psycopg.OperationalError)
    except Exception:
        log.exception("Aggregation failed day=%s elapsed_seconds=%.3f", day, monotonic() - start)
        raise
    log.info("Aggregation finished day=%s events_processed=%s updated=%s elapsed_seconds=%.3f", day, count, changed, monotonic() - start)
    return {"date": day, "events_processed": count, "updated": changed}


def retention_rates(day, today, cohort_size, retained):
    return [
        retained[offset] / cohort_size
        if cohort_size and (offset == 0 or day + timedelta(days=offset) < today) else None
        for offset in range(RETENTION_DAYS + 1)
    ]


def aggregate_day(connection, ch, day, force):
    daily = ch.rows(DAILY_SQL, day=day.isoformat(), max_movie_id_bytes=MAX_MOVIE_ID_BYTES)[0]
    cohort = ch.rows(COHORT_SQL, day=day.isoformat(), retention_window=RETENTION_DAYS + 1)[0]
    today = utc_today()
    cohort_size = cohort["cohort_size"]
    activity_cohort_size = cohort["activity_cohort_size"]
    retention = retention_rates(day, today, cohort_size, cohort["retained"])
    activity_retention = retention_rates(day, today, activity_cohort_size, cohort["activity_retained"])
    metrics = {(name, ""): daily[name] for name in ("dau", "avg_watch_seconds", "view_conversion")}
    metrics["cohort_size", ""] = cohort_size
    metrics["activity_cohort_size", ""] = activity_cohort_size
    metrics.update({(f"retention_d{i}", ""): value for i, value in enumerate(retention)})
    metrics.update({(f"activity_retention_d{i}", ""): value for i, value in enumerate(activity_retention)})
    metrics.update({("movie_views", movie): views for movie, views in daily["top_movies"]})
    if not force:
        existing = connection.execute("SELECT metric, movie_id, value FROM daily_metrics WHERE day = %s", (day,)).fetchall()
        if metrics == {(metric, movie): value for metric, movie, value in existing}:
            return daily["events_processed"], False

    computed_at = datetime.now(UTC)
    snapshot = {
        "day": day.isoformat(),
        "dau": daily["dau"],
        "avg_watch_seconds": daily["avg_watch_seconds"],
        "view_conversion": daily["view_conversion"],
        "top_movies": [{"movie_id": movie, "views": views} for movie, views in daily["top_movies"]],
        "cohort_size": cohort_size,
        "retention": retention,
        "activity_cohort_size": activity_cohort_size,
        "activity_retention": activity_retention,
        "computed_at": computed_at.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds"),
    }
    ch.execute("INSERT INTO cinema.daily_aggregates FORMAT JSONEachRow", json.dumps(snapshot, allow_nan=False))
    with connection.cursor() as cursor:
        cursor.executemany(UPSERT_SQL, [(day, metric, movie, value, computed_at) for (metric, movie), value in metrics.items()])
    connection.execute("DELETE FROM daily_metrics WHERE day = %s AND computed_at <> %s", (day, computed_at))
    return daily["events_processed"], True
