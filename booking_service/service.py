"""Booking workflow with a durable compensation/cancellation journal."""
from decimal import Decimal
import logging
from uuid import uuid4
import grpc
from common.db import connect
from common.errors import fail

log = logging.getLogger(__name__)


def booking_json(row):
    return {**row, "id":str(row["id"]), "flight_id":str(row["flight_id"]),
            "total_price":format(row["total_price"], ".2f"),
            "created_at":row["created_at"].isoformat()}


class BookingService:
    def __init__(self, dsn, client, recovery_grace=30):
        self.dsn, self.client = dsn, client
        self.recovery_grace = recovery_grace

    def _save_booking(self, conn, booking_id, data, price):
        return conn.execute("""INSERT INTO bookings
            (id,user_id,flight_id,passenger_name,passenger_email,seat_count,total_price)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (booking_id,data["user_id"],data["flight_id"],data["passenger_name"],
             data["passenger_email"],data["seat_count"],price)).fetchone()

    def create(self, data):
        bid = str(uuid4())
        # Commit intent before any RPC: a killed process cannot leak a reservation.
        with connect(self.dsn) as conn:
            conn.execute("INSERT INTO booking_operations(booking_id,kind) VALUES (%s,'CREATE')", (bid,))
        try:
            with connect(self.dsn) as conn:
                op = conn.execute("SELECT * FROM booking_operations WHERE booking_id=%s FOR UPDATE", (bid,)).fetchone()
                if op["completed"]:
                    fail(grpc.StatusCode.ABORTED, "Booking attempt was already compensated; try again")
                flight = self.client.get_flight(data["flight_id"])
                self.client.reserve(data["flight_id"], data["seat_count"], bid, flight.price)
                row = self._save_booking(conn, bid, data, Decimal(flight.price) * data["seat_count"])
                conn.execute("UPDATE booking_operations SET completed=true WHERE booking_id=%s", (bid,))
            return booking_json(row)
        except Exception:
            try:
                self._finish_operation(bid)
            except Exception:
                log.exception("booking_compensation_deferred booking_id=%s", bid)
            raise

    def get(self, id):
        with connect(self.dsn) as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id=%s", (id,)).fetchone()
        if not row:
            fail(grpc.StatusCode.NOT_FOUND, "Booking not found")
        return booking_json(row)

    def list(self, user_id):
        with connect(self.dsn) as conn:
            rows = conn.execute("SELECT * FROM bookings WHERE user_id=%s ORDER BY created_at,id", (user_id,)).fetchall()
        return [booking_json(row) for row in rows]

    def cancel(self, id):
        booking = self.get(id)
        if booking["status"] == "CANCELLED":
            return booking
        # Persist cancellation BEFORE Release; a lost response is retried by worker.
        with connect(self.dsn) as conn:
            conn.execute("""INSERT INTO booking_operations(booking_id,kind) VALUES (%s,'CANCEL')
                ON CONFLICT (booking_id) DO UPDATE SET kind='CANCEL',completed=false,created_at=now()""", (id,))
        self._finish_operation(id)
        return self.get(id)

    def _finish_operation(self, booking_id, *, skip_locked=False):
        booking_id = str(booking_id)
        with connect(self.dsn) as conn:
            suffix = " SKIP LOCKED" if skip_locked else ""
            op = conn.execute("SELECT * FROM booking_operations WHERE booking_id=%s FOR UPDATE" + suffix, (booking_id,)).fetchone()
            if not op or op["completed"]:
                return
            booking = conn.execute("SELECT * FROM bookings WHERE id=%s FOR UPDATE", (booking_id,)).fetchone()
            if op["kind"] == "CREATE":
                if not booking:
                    self.client.release(booking_id)
            elif not booking or booking["status"] == "CONFIRMED":
                self.client.release(booking_id)
                if booking:
                    conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=%s", (booking_id,))
            conn.execute("UPDATE booking_operations SET completed=true WHERE booking_id=%s", (booking_id,))
            log.info("booking_operation_recovered booking_id=%s kind=%s", booking_id, op["kind"])

    def recover_once(self):
        with connect(self.dsn) as conn:
            rows = conn.execute("""SELECT booking_id FROM booking_operations
                WHERE NOT completed AND created_at <= now() - (%s * interval '1 second')
                ORDER BY created_at LIMIT 100""", (self.recovery_grace,)).fetchall()
        for row in rows:
            try:
                self._finish_operation(row["booking_id"], skip_locked=True)
            except Exception:
                log.exception("booking_recovery_retry_later booking_id=%s", row["booking_id"])

