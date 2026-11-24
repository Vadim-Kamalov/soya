import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from time import gmtime

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "movie-events"
SUBJECT = f"{TOPIC}-value"
BROKERS = os.getenv("KAFKA_BROKERS", "kafka-1:9092,kafka-2:9092,kafka-3:9092")
REGISTRY = os.getenv("SCHEMA_REGISTRY_URL", "http://schema-registry:8081")
MAX_MOVIE_ID_BYTES = 1024
RETENTION_DAYS = 7

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.Formatter.converter = gmtime


def utc_today():
    return datetime.now(UTC).date()
