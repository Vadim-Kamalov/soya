from contextlib import contextmanager
from uuid import uuid4
import grpc
from common.db import connect
from common.errors import fail

FLIGHT_SQL = "SELECT f.*, a.name AS airline FROM flights f JOIN airlines a ON a.code=f.airline_code"


class FlightRepository:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def snapshot(self):
        with connect(self.dsn) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield conn

    def revision(self, conn):
        return conn.execute("SELECT revision FROM cache_revision").fetchone()["revision"]

    def get(self, conn, id):
        row = conn.execute(FLIGHT_SQL + " WHERE f.id=%s", (id,)).fetchone()
        if not row:
            fail(grpc.StatusCode.NOT_FOUND, "Flight not found")
        return row

    def search(self, conn, origin, destination, date):
        sql = FLIGHT_SQL + " WHERE f.origin=%s AND f.destination=%s AND f.status='SCHEDULED'"
        params = [origin, destination]
        if date:
            sql += " AND f.departure_at >= %s AND f.departure_at < (%s::timestamptz + interval '24 hours')"
            params.extend([date, date])
        return conn.execute(sql + " ORDER BY f.departure_at,f.id", params).fetchall()

    @staticmethod
    def _lock_booking(conn, booking_id):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (booking_id,))

    @staticmethod
    def _bump(conn):
        conn.execute("UPDATE cache_revision SET revision=revision+1")

    @staticmethod
    def _locked_flight(conn, flight_id):
        row = conn.execute("SELECT * FROM flights WHERE id=%s FOR UPDATE", (flight_id,)).fetchone()
        if not row:
            fail(grpc.StatusCode.NOT_FOUND, "Flight not found")
        return row

    def reserve(self, flight_id, count, booking_id, expected_price=None):
        with connect(self.dsn) as conn:
            self._lock_booking(conn, booking_id)
            if conn.execute("SELECT 1 FROM reservation_cancellations WHERE booking_id=%s", (booking_id,)).fetchone():
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Reservation has already been cancelled")
            existing = conn.execute("SELECT * FROM seat_reservations WHERE booking_id=%s", (booking_id,)).fetchone()
            if existing:
                if str(existing["flight_id"]) != flight_id or existing["seat_count"] != count:
                    fail(grpc.StatusCode.ALREADY_EXISTS, "booking_id already used with different parameters")
                if existing["status"] != "ACTIVE":
                    fail(grpc.StatusCode.FAILED_PRECONDITION, "Reservation is no longer active")
                if expected_price is not None and existing["unit_price"] != expected_price:
                    fail(grpc.StatusCode.ALREADY_EXISTS, "booking_id already used with another price")
                return existing
            flight = self._locked_flight(conn, flight_id)
            if flight["status"] != "SCHEDULED":
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Flight is not scheduled")
            if expected_price is not None and flight["price"] != expected_price:
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Flight price changed; request a fresh quote")
            if flight["available_seats"] < count:
                fail(grpc.StatusCode.RESOURCE_EXHAUSTED, "Not enough available seats")
            conn.execute("UPDATE flights SET available_seats=available_seats-%s WHERE id=%s", (count, flight_id))
            row = conn.execute("""INSERT INTO seat_reservations
                (id,booking_id,flight_id,seat_count,unit_price,status) VALUES (%s,%s,%s,%s,%s,'ACTIVE') RETURNING *""",
                (uuid4(), booking_id, flight_id, count, flight["price"])).fetchone()
            self._bump(conn)
            return row

    def release(self, booking_id):
        with connect(self.dsn) as conn:
            self._lock_booking(conn, booking_id)
            conn.execute("INSERT INTO reservation_cancellations(booking_id) VALUES (%s) ON CONFLICT DO NOTHING", (booking_id,))
            row = conn.execute("SELECT * FROM seat_reservations WHERE booking_id=%s", (booking_id,)).fetchone()
            if not row or row["status"] != "ACTIVE":
                return False
            self._locked_flight(conn, row["flight_id"])
            conn.execute("UPDATE flights SET available_seats=available_seats+%s WHERE id=%s", (row["seat_count"], row["flight_id"]))
            conn.execute("UPDATE seat_reservations SET status='RELEASED' WHERE booking_id=%s", (booking_id,))
            self._bump(conn)
            return True

    def update(self, flight_id, price=None, capacity=None, status=None):
        with connect(self.dsn) as conn:
            flight = self._locked_flight(conn, flight_id)
            used = flight["total_seats"] - flight["available_seats"]
            capacity = capacity if capacity is not None else flight["total_seats"]
            if capacity < used:
                fail(grpc.StatusCode.FAILED_PRECONDITION, "Capacity cannot be less than reserved seats")
            conn.execute("UPDATE flights SET price=%s,total_seats=%s,available_seats=%s,status=%s WHERE id=%s",
                (price if price is not None else flight["price"], capacity, capacity-used,
                 status or flight["status"], flight_id))
            self._bump(conn)
            return self.get(conn, flight_id)

