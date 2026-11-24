import json
import os
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic, sleep
from uuid import uuid4

import requests

PRODUCER = os.getenv("PRODUCER_URL", "http://producer:8000")
AGGREGATOR = os.getenv("AGGREGATOR_URL", "http://aggregation:8001")
GRAFANA = os.getenv("GRAFANA_URL", "http://grafana:3000")


def send(user, movie, event_type, when, progress=0, session_id=None):
    event = {
        "event_id": str(uuid4()), "user_id": user, "movie_id": movie,
        "event_type": event_type, "timestamp": when.isoformat(), "device_type": "TV",
        "session_id": session_id or f"{user}-{when.date()}", "progress_seconds": progress,
    }
    response = requests.post(f"{PRODUCER}/events", json=event, timeout=45)
    response.raise_for_status()
    assert response.status_code == 201 and response.json()["event_id"] == event["event_id"]
    return event


def wait_for_event(ch, event):
    deadline = monotonic() + 45
    while monotonic() < deadline:
        rows = ch.rows("""
            SELECT event_id, user_id, movie_id, event_type, timestamp, device_type,
                   session_id, progress_seconds, kafka_key
            FROM cinema.raw_events FINAL
            WHERE event_date = {day:Date32} AND event_id = {id:UUID}
            """, day=event["timestamp"][:10], id=event["event_id"])
        if rows:
            return rows
        sleep(0.5)
    raise AssertionError(f"Event {event['event_id']} did not reach ClickHouse")


class IntegrationTests(unittest.TestCase):
    def test_event_round_trip(self):
        from cinema.storage import ClickHouse

        when = datetime(1901, 8, 9, 13, 4, 5, 123456, tzinfo=UTC)
        event = send(f"test-{uuid4()}", "film", "VIEW_FINISHED", when, 891)
        with ClickHouse() as ch:
            rows = wait_for_event(ch, event)
        self.assertEqual(len(rows), 1)
        timestamp = datetime.fromisoformat(rows[0].pop("timestamp")).replace(tzinfo=UTC)
        self.assertEqual(timestamp, when)
        self.assertEqual(rows[0].pop("kafka_key"), event["user_id"])
        del event["timestamp"]
        self.assertEqual(rows[0], event)
        response = requests.post(f"{PRODUCER}/events", json=event, timeout=5)
        self.assertEqual(response.status_code, 422)

    def test_metrics_retention_and_idempotent_export(self):
        from cinema.export import BUCKET, s3_client
        from cinema.storage import ClickHouse, pg_connect

        run_id = uuid4()
        day = date(2000, 1, 15)
        viewing_retention = [1, .5, 0, 0, 0, 0, 0, .5]
        activity_retention = [1, 0, .5, 0, 0, 0, 0, .5]
        with ClickHouse() as ch:
            a, b, c = (f"test-{run_id}-{i}" for i in range(3))
            start = datetime.combine(day, time(8), tzinfo=UTC)
            for user, movie, kind, offset, seconds in (
                (a, "film-a", "SEARCHED", -1, 0),
                (a, "film-a", "VIEW_STARTED", 0, 0), (b, "film-b", "VIEW_STARTED", 0, 0),
                (b, "film-b", "VIEW_FINISHED", 0, 120), (c, "film-b", "LIKED", 0, 0),
                (a, "film-a", "VIEW_RESUMED", 1, 120), (c, "film-b", "VIEW_STARTED", 2, 0),
                (b, "film-b", "LIKED", 7, 0),
            ):
                when = start + timedelta(days=offset, seconds=seconds)
                wait_for_event(ch, send(user, movie, kind, when, seconds, session_id=f"{user}-session"))
            for _ in range(2):
                response = requests.post(f"{AGGREGATOR}/aggregate/{day}", timeout=60)
                response.raise_for_status()
                self.assertEqual(response.json()["events_processed"], 4)
                response = requests.post(f"{AGGREGATOR}/export/{day}", timeout=60)
                response.raise_for_status()
                self.assertEqual(response.json()["key"], f"daily/{day}/aggregates.json")

            saved = ch.rows("""
                SELECT dau, cohort_size, retention, activity_cohort_size, activity_retention
                FROM cinema.daily_aggregates FINAL
                WHERE day = {day:Date32}
                """, day=day.isoformat())
            self.assertEqual(saved, [{
                "dau": 3, "cohort_size": 2, "retention": viewing_retention,
                "activity_cohort_size": 2, "activity_retention": activity_retention,
            }])

        with pg_connect() as connection:
            rows = connection.execute("""
                SELECT metric, movie_id, value, computed_at FROM daily_metrics WHERE day = %s
                """, (day,)).fetchall()
        values = {(metric, movie): value for metric, movie, value, _ in rows}
        self.assertEqual(len(rows), len(values))
        self.assertEqual(values, {
            ("dau", ""): 3, ("avg_watch_seconds", ""): 120, ("view_conversion", ""): .5,
            ("cohort_size", ""): 2, ("movie_views", "film-a"): 1, ("movie_views", "film-b"): 1,
            ("activity_cohort_size", ""): 2,
            **{(f"retention_d{i}", ""): value for i, value in enumerate(viewing_retention)},
            **{(f"activity_retention_d{i}", ""): value for i, value in enumerate(activity_retention)},
        })
        key = f"daily/{day}/aggregates.json"
        with closing(s3_client()) as s3:
            self.assertEqual(s3.list_objects_v2(Bucket=BUCKET, Prefix=f"daily/{day}/")["KeyCount"], 1)
            with s3.get_object(Bucket=BUCKET, Key=key)["Body"] as body:
                exported = json.load(body)
        self.assertEqual(exported["date"], day.isoformat())
        self.assertEqual({(row["metric"], row["movie_id"]): row["value"] for row in exported["metrics"]}, values)
        computed_at = {row[3] for row in rows}
        self.assertEqual({datetime.fromisoformat(row["computed_at"]) for row in exported["metrics"]}, computed_at)

        self.check_dashboard(day)

    def check_dashboard(self, day):
        response = requests.get(f"{GRAFANA}/api/dashboards/uid/cinema-analytics", timeout=10)
        response.raise_for_status()
        panels = response.json()["dashboard"]["panels"]
        self.assertGreaterEqual(len(panels), 3)
        queries = [panel["targets"][0] | {"refId": str(panel["id"])} for panel in panels]
        start = datetime.combine(day, time.min, tzinfo=UTC)
        response = requests.post(f"{GRAFANA}/api/ds/query", json={
            "from": str(int(start.timestamp()) * 1000),
            "to": str(int((start + timedelta(days=1)).timestamp()) * 1000 - 1),
            "queries": queries,
        }, timeout=25)
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()["results"]
        for query in queries:
            result = results[query["refId"]]
            self.assertEqual(result.get("status", 200), 200, result)
            self.assertTrue(result["frames"][0]["data"]["values"][0], result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
