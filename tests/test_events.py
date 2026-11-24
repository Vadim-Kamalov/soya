import random
import unittest
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from fastavro import schemaless_reader
from pydantic import ValidationError

from cinema.events import AVRO_SCHEMA, Event, encode
from cinema.generator import playback_session


class EventTests(unittest.TestCase):
    payload = {
        "event_id": "57fda40d-65d1-486a-87ac-9877132e2e0c",
        "user_id": "user-1",
        "movie_id": "movie-1",
        "event_type": "VIEW_FINISHED",
        "timestamp": "2026-04-03T03:30:00.123456+03:00",
        "device_type": "TV",
        "session_id": "session-1",
        "progress_seconds": 1345,
    }

    def test_avro_preserves_typed_fields_and_normalizes_utc(self):
        data = encode(Event.model_validate(self.payload), 42)
        self.assertEqual(data[:5], b"\x00\x00\x00\x00\x2a")
        decoded = schemaless_reader(BytesIO(data[5:]), AVRO_SCHEMA)
        self.assertEqual(decoded, {
            **self.payload,
            "event_id": UUID(self.payload["event_id"]),
            "timestamp": datetime(2026, 4, 3, 0, 30, 0, 123456, tzinfo=UTC),
        })

    def test_rejects_values_outside_the_contract(self):
        for invalid in (
            {"event_id": "not-uuid"}, {"event_type": "VIEW"}, {"device_type": "WATCH"},
            {"user_id": " "}, {"timestamp": "2026-04-03T03:30:00"}, {"timestamp": 100},
            {"movie_id": "movie\x00id"}, {"movie_id": "я" * 513},
            {"progress_seconds": -1}, {"progress_seconds": 2**31}, {"progress_seconds": "10"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                Event.model_validate(self.payload | invalid)

    def test_generator_preserves_playback_order_and_progress(self):
        rng = random.Random(194)
        finished = paused = 0
        for _ in range(100):
            events = playback_session(rng, "user", "film", datetime(2026, 4, 3, tzinfo=UTC))
            playback = [event for event in events if event.event_type.startswith("VIEW_")]
            types = [event.event_type for event in playback]
            self.assertIn(types, [
                ["VIEW_STARTED"], ["VIEW_STARTED", "VIEW_FINISHED"],
                ["VIEW_STARTED", "VIEW_PAUSED", "VIEW_RESUMED"],
                ["VIEW_STARTED", "VIEW_PAUSED", "VIEW_RESUMED", "VIEW_FINISHED"],
            ])
            self.assertEqual(len({event.session_id for event in events}), 1)
            self.assertEqual(len({event.event_id for event in events}), len(events))
            self.assertEqual([event.timestamp for event in events], sorted(event.timestamp for event in events))
            self.assertEqual([event.progress_seconds for event in playback], sorted(event.progress_seconds for event in playback))
            finished += "VIEW_FINISHED" in types
            paused += "VIEW_PAUSED" in types
        self.assertGreater(finished, 0)
        self.assertGreater(paused, 0)
