CREATE DATABASE IF NOT EXISTS cinema;

CREATE TABLE IF NOT EXISTS cinema.raw_events
(
    event_id UUID,
    user_id String,
    movie_id String,
    event_type Enum8('VIEW_STARTED' = 1, 'VIEW_FINISHED' = 2, 'VIEW_PAUSED' = 3, 'VIEW_RESUMED' = 4, 'LIKED' = 5, 'SEARCHED' = 6),
    timestamp DateTime64(6, 'UTC'),
    device_type Enum8('MOBILE' = 1, 'DESKTOP' = 2, 'TV' = 3, 'TABLET' = 4),
    session_id String,
    progress_seconds Int32,
    event_date Date32 MATERIALIZED toDate32(timestamp),
    kafka_key String
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(event_date)
ORDER BY (event_date, user_id, event_id);

CREATE TABLE IF NOT EXISTS cinema.daily_aggregates
(
    day Date32,
    dau UInt64,
    avg_watch_seconds Nullable(Float64),
    view_conversion Nullable(Float64),
    top_movies Array(Tuple(movie_id String, views UInt64)),
    cohort_size UInt64,
    retention Array(Nullable(Float64)),
    computed_at DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(day)
ORDER BY day;

ALTER TABLE cinema.daily_aggregates
    ADD COLUMN IF NOT EXISTS activity_cohort_size UInt64,
    ADD COLUMN IF NOT EXISTS activity_retention Array(Nullable(Float64));

CREATE USER IF NOT EXISTS grafana_reader;
GRANT SELECT ON cinema.daily_aggregates TO grafana_reader;
