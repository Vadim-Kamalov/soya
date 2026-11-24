CREATE TABLE IF NOT EXISTS cinema.kafka_events
(
    event_id UUID,
    user_id String,
    movie_id String,
    event_type Enum8('VIEW_STARTED' = 1, 'VIEW_FINISHED' = 2, 'VIEW_PAUSED' = 3, 'VIEW_RESUMED' = 4, 'LIKED' = 5, 'SEARCHED' = 6),
    timestamp DateTime64(6, 'UTC'),
    device_type Enum8('MOBILE' = 1, 'DESKTOP' = 2, 'TV' = 3, 'TABLET' = 4),
    session_id String,
    progress_seconds Int32
)
ENGINE = Kafka
SETTINGS kafka_broker_list = 'kafka-1:9092,kafka-2:9092,kafka-3:9092',
         kafka_topic_list = 'movie-events',
         kafka_group_name = 'movie-events-clickhouse',
         kafka_format = 'AvroConfluent',
         format_avro_schema_registry_url = 'http://schema-registry:8081',
         kafka_flush_interval_ms = 500;

CREATE MATERIALIZED VIEW IF NOT EXISTS cinema.ingest_events TO cinema.raw_events AS
SELECT event_id, user_id, movie_id, event_type, timestamp, device_type,
       session_id, progress_seconds, _key AS kafka_key
FROM cinema.kafka_events;
