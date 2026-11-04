CREATE TABLE bookings (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    flight_id UUID NOT NULL,
    passenger_name TEXT NOT NULL CHECK (length(trim(passenger_name)) > 0),
    passenger_email TEXT NOT NULL CHECK (passenger_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'),
    seat_count INTEGER NOT NULL CHECK (seat_count > 0),
    total_price NUMERIC(22,2) NOT NULL CHECK (total_price > 0),
    status TEXT NOT NULL DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED','CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX bookings_user ON bookings(user_id,created_at);
-- No booking is exposed until both Reserve and the local commit succeed.
CREATE TABLE booking_operations (
    booking_id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('CREATE','CANCEL')),
    completed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX operations_pending ON booking_operations(created_at) WHERE NOT completed;

