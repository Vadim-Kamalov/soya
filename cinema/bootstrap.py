import argparse
import logging

import psycopg
import requests
from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic

from .events import SCHEMA_TEXT
from .settings import BROKERS, REGISTRY, ROOT, SUBJECT, TOPIC
from .storage import ClickHouse, pg_connect, retry

log = logging.getLogger(__name__)


def create_topic():
    admin = AdminClient({"bootstrap.servers": BROKERS, "allow.auto.create.topics": False})
    topics = [NewTopic(TOPIC, num_partitions=3, replication_factor=3, config={"min.insync.replicas": "2"})]
    try:
        admin.create_topics(topics, operation_timeout=10, request_timeout=15)[TOPIC].result(timeout=16)
    except KafkaException as exc:
        if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
            raise
    topic = admin.list_topics(topic=TOPIC, timeout=10).topics[TOPIC]
    if topic.error or len(topic.partitions) < 3 or any(len(p.replicas) < 3 or p.leader < 0 for p in topic.partitions.values()):
        raise RuntimeError(f"{TOPIC} needs three ready partitions with three replicas each")
    resource = ConfigResource("topic", TOPIC)
    configs = admin.describe_configs([resource], request_timeout=10)[resource].result(timeout=11)
    if int(configs["min.insync.replicas"].value) < 2:
        raise RuntimeError(f"{TOPIC} needs min.insync.replicas >= 2")
    log.info("Kafka topic %s ready with %s partitions", TOPIC, len(topic.partitions))


def register_schema():
    with requests.Session() as session:
        response = session.put(f"{REGISTRY}/config/{SUBJECT}", json={"compatibility": "BACKWARD_TRANSITIVE"}, timeout=10)
        response.raise_for_status()
        url = f"{REGISTRY}/subjects/{SUBJECT}"
        response = session.post(f"{url}/versions", json={"schema": SCHEMA_TEXT}, timeout=10)
        response.raise_for_status()
        response = session.post(url, json={"schema": SCHEMA_TEXT}, timeout=10)
        response.raise_for_status()
        info = response.json()
    log.info("Registered Avro subject=%s version=%s id=%s", SUBJECT, info["version"], info["id"])


def migrate_clickhouse(filename):
    with ClickHouse() as ch:
        sql = (ROOT / "sql" / filename).read_text(encoding="utf-8")
        for statement in sql.split(";"):
            if statement.strip():
                ch.execute(statement.strip())


def migrate_databases():
    migrate_clickhouse("clickhouse.sql")
    with pg_connect() as connection:
        connection.execute((ROOT / "sql/postgres.sql").read_text(encoding="utf-8"))
    log.info("ClickHouse and PostgreSQL migrations applied")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("databases", "ingestion"))
    if parser.parse_args().stage == "databases":
        retry(migrate_databases, (requests.RequestException, psycopg.OperationalError), attempts=6)
    else:
        retry(create_topic, (KafkaException, RuntimeError, TimeoutError), attempts=10)
        retry(register_schema, requests.RequestException, attempts=10)
        retry(lambda: migrate_clickhouse("clickhouse-ingestion.sql"), requests.RequestException, attempts=6)
