CREATE TABLE airlines (
    code VARCHAR(3) PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
);
CREATE TABLE airports (
    code CHAR(3) PRIMARY KEY CHECK (code ~ '^[A-Z]{3}$'),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
);
CREATE TABLE flights (
    id UUID PRIMARY KEY,
    flight_number TEXT NOT NULL CHECK (length(trim(flight_number)) > 0),
    airline_code VARCHAR(3) NOT NULL REFERENCES airlines(code),
    origin CHAR(3) NOT NULL REFERENCES airports(code),
    destination CHAR(3) NOT NULL REFERENCES airports(code),
    departure_at TIMESTAMPTZ NOT NULL,
    arrival_at TIMESTAMPTZ NOT NULL CHECK (arrival_at > departure_at),
    total_seats INTEGER NOT NULL CHECK (total_seats > 0),
    available_seats INTEGER NOT NULL CHECK (available_seats BETWEEN 0 AND total_seats),
    price NUMERIC(12,2) NOT NULL CHECK (price > 0),
    status TEXT NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED','DEPARTED','CANCELLED','COMPLETED')),
    CHECK (origin <> destination)
);
CREATE UNIQUE INDEX flights_number_date ON flights(flight_number, ((departure_at AT TIME ZONE 'UTC')::date));
CREATE INDEX flights_search ON flights(origin,destination,departure_at) WHERE status='SCHEDULED';
CREATE TABLE seat_reservations (
    id UUID PRIMARY KEY,
    booking_id UUID NOT NULL UNIQUE,
    flight_id UUID NOT NULL REFERENCES flights(id),
    seat_count INTEGER NOT NULL CHECK (seat_count > 0),
    unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX reservations_flight ON seat_reservations(flight_id);
-- A durable fence against Reserve arriving after compensation/Release.
CREATE TABLE reservation_cancellations (
    booking_id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE cache_revision (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0)
);
INSERT INTO cache_revision DEFAULT VALUES;

