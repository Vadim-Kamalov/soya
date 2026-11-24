import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, patch

import psycopg

from cinema import aggregation, export
from cinema.metrics import retention_rates


class AggregationTests(unittest.TestCase):
    def test_seven_day_lookback_revisits_cohort_after_day_seven_ends(self):
        today = date(2026, 4, 9)
        stop = Event()
        with (
            patch.object(aggregation, "utc_today", return_value=today),
            patch.object(aggregation, "calculate") as calculate,
            patch.object(aggregation, "unprocessed_days", return_value=[]),
            patch.object(aggregation, "pending_days", return_value=[]),
            patch.object(stop, "wait", side_effect=lambda _: stop.set()),
        ):
            aggregation.schedule(stop, interval=60, lookback=7)

        calculate.assert_any_call(today - timedelta(days=8), force=False)
        cohort = today - timedelta(days=8)
        retained = [2, 0, 0, 0, 0, 0, 0, 1]
        self.assertIsNone(retention_rates(cohort, today - timedelta(days=1), 2, retained)[7])
        self.assertEqual(retention_rates(cohort, today, 2, retained)[7], .5)

    def test_manual_export_survives_postgres_outage_and_retries_today(self):
        day = date(2026, 4, 9)
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []
        pg = MagicMock()
        pg.__enter__.return_value = connection
        with TemporaryDirectory() as temp_dir:
            with (
                patch.object(export, "EXPORT_REQUESTS_DIR", Path(temp_dir)),
                patch.object(export, "pg_connect", side_effect=psycopg.OperationalError),
                self.assertRaises(psycopg.OperationalError),
            ):
                export.export_day(day, requested=True)

            with (
                patch.object(export, "EXPORT_REQUESTS_DIR", Path(temp_dir)),
                patch.object(export, "pg_connect", return_value=pg),
                patch.object(export, "_export_snapshot", return_value={"key": "example"}),
            ):
                self.assertEqual(export.pending_days(day), [day])
                self.assertEqual(export.export_day(day), {"key": "example"})
                self.assertEqual(export.pending_days(day), [])

    def test_failed_repeat_export_remains_pending_until_s3_recovers(self):
        day = date(2026, 4, 8)
        computed_at = datetime(2026, 4, 9, tzinfo=UTC)
        pg = MagicMock()
        connection = pg.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (computed_at,)
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.return_value.fetchall.side_effect = [
            [{"metric": "dau", "movie_id": "", "value": 1, "computed_at": computed_at}]
            for _ in range(2)
        ]
        s3 = MagicMock()
        s3.put_object.side_effect = [ConnectionError("S3 unavailable"), {}]

        with TemporaryDirectory() as temp_dir:
            with (
                patch.object(export, "EXPORT_REQUESTS_DIR", Path(temp_dir)),
                patch.object(export, "pg_connect", return_value=pg),
                patch.object(export, "s3_client", return_value=s3),
            ):
                with self.assertRaises(ConnectionError):
                    export.export_day(day, requested=True)
                self.assertEqual(export.pending_days(day + timedelta(days=1)), [day])
                response = export.export_day(day)
                self.assertEqual(response["key"], f"daily/{day}/aggregates.json")
                self.assertEqual(export.pending_days(day + timedelta(days=1)), [])
