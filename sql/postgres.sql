CREATE TABLE IF NOT EXISTS daily_metrics (
    day date NOT NULL,
    metric text NOT NULL,
    movie_id text NOT NULL DEFAULT '',
    value double precision,
    computed_at timestamptz NOT NULL,
    PRIMARY KEY (day, metric, movie_id)
);

CREATE TABLE IF NOT EXISTS daily_exports (
    day date PRIMARY KEY,
    computed_at timestamptz NOT NULL,
    exported_at timestamptz NOT NULL DEFAULT now()
);
