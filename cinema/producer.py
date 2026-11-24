import logging
import os
from contextlib import asynccontextmanager
from threading import Event as Signal, Thread

import requests
from confluent_kafka import KafkaException, Producer
from fastapi import FastAPI, HTTPException, Request
from fastavro.validation import ValidationError as AvroValidationError

from .events import SCHEMA_TEXT, Event, encode
from .generator import generate
from .settings import BROKERS, REGISTRY, SUBJECT, TOPIC

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self):
        response = requests.post(f"{REGISTRY}/subjects/{SUBJECT}", json={"schema": SCHEMA_TEXT}, timeout=10)
        response.raise_for_status()
        self.schema_id = response.json()["id"]
        self.kafka = Producer({
            "bootstrap.servers": BROKERS,
            "allow.auto.create.topics": False,
            "partitioner": "murmur2_random",
            "acks": "all",
            "enable.idempotence": True,
            "retry.backoff.ms": 200,
            "retry.backoff.max.ms": 4000,
            "message.timeout.ms": 30000,
            "linger.ms": 5,
        })

    def publish(self, event):
        payload = encode(event, self.schema_id)
        delivered = Signal()
        error = None

        def acknowledge(result, _message):
            nonlocal error
            error = result
            delivered.set()

        for delay in (0.2, 0.4, 0.8, None):
            try:
                self.kafka.produce(TOPIC, key=event.user_id, value=payload, on_delivery=acknowledge)
                break
            except BufferError:
                if delay is None:
                    raise
                self.kafka.poll(delay)

        while not delivered.is_set():
            self.kafka.poll(0.2)
        if error is not None:
            raise KafkaException(error)

        log.info("Published event_id=%s event_type=%s timestamp=%s", event.event_id, event.event_type, event.timestamp.isoformat())


@asynccontextmanager
async def lifespan(app):
    publisher = app.state.publisher = Publisher()
    stop = Signal()
    worker = None
    if os.getenv("GENERATOR_ENABLED", "true").lower() == "true":
        interval = float(os.getenv("GENERATOR_INTERVAL_SECONDS", "3"))
        if interval <= 0:
            raise ValueError("GENERATOR_INTERVAL_SECONDS must be positive")
        worker = Thread(target=generate, args=(publisher.publish, stop, interval), daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker:
            worker.join(timeout=32)
        publisher.kafka.flush(2)


app = FastAPI(title="Movie Service", lifespan=lifespan)


@app.get("/health")
def health(request: Request):
    try:
        topic = request.app.state.publisher.kafka.list_topics(topic=TOPIC, timeout=2).topics.get(TOPIC)
        if topic and topic.error is None and topic.partitions and all(p.leader >= 0 for p in topic.partitions.values()):
            return {"status": "ok"}
    except KafkaException:
        log.warning("Kafka health probe failed", exc_info=True)
    raise HTTPException(503, "Kafka topic is unavailable")


@app.post("/events", status_code=201)
def post_event(event: Event, request: Request):
    try:
        request.app.state.publisher.publish(event)
    except AvroValidationError as exc:
        raise HTTPException(422, "Event does not match the Avro schema") from exc
    except (KafkaException, BufferError) as exc:
        log.warning("Publishing failed event_id=%s", event.event_id, exc_info=True)
        raise HTTPException(503, "Kafka did not acknowledge the event") from exc
    return {"event_id": str(event.event_id)}
