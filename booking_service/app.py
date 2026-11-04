from contextlib import asynccontextmanager
from datetime import date
import logging
import os
import threading
from typing import Annotated
from uuid import UUID
import grpc
import psycopg
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from common.db import connect, migrate
from common.errors import BusinessError
from generated import flight_pb2 as pb
from booking_service.service import BookingService
from booking_service.grpc_client import FlightClient

log = logging.getLogger(__name__)


class CreateBooking(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=200)
    flight_id: UUID
    passenger_name: str = Field(min_length=1, max_length=200)
    passenger_email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    seat_count: StrictInt = Field(gt=0, le=2147483647)


def flight_json(flight):
    return {"id":flight.id, "flight_number":flight.flight_number,"airline":flight.airline,
        "origin":flight.origin,"destination":flight.destination,
        "departure_at":flight.departure_at.ToJsonString(),"arrival_at":flight.arrival_at.ToJsonString(),
        "total_seats":flight.total_seats,"available_seats":flight.available_seats,
        "price":flight.price,"status":pb.FlightStatus.Name(flight.status)}


def http_error(code, detail):
    status = {grpc.StatusCode.INVALID_ARGUMENT:400, grpc.StatusCode.NOT_FOUND:404,
        grpc.StatusCode.RESOURCE_EXHAUSTED:409,grpc.StatusCode.FAILED_PRECONDITION:409,
        grpc.StatusCode.ALREADY_EXISTS:409,grpc.StatusCode.ABORTED:409,
        grpc.StatusCode.UNAVAILABLE:503,grpc.StatusCode.DEADLINE_EXCEEDED:503}.get(code, 502)
    return JSONResponse(status_code=status, content={"detail":detail})


def create_app(service=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        owned = service is None
        actual = service
        if owned:
            dsn = os.environ["BOOKING_DATABASE_URL"]
            migrate(dsn, "booking")
            actual = BookingService(dsn, FlightClient(), float(os.getenv("RECOVERY_GRACE_SECONDS", "30")))
        app.state.service = actual
        stop = threading.Event()
        interval = float(os.getenv("RECOVERY_INTERVAL_SECONDS", "5"))
        if interval <= 0 or actual.recovery_grace < 0:
            raise ValueError("Invalid recovery settings")
        def recover():
            while not stop.is_set():
                try:
                    actual.recover_once()
                except Exception:
                    log.exception("booking_recovery_iteration_failed")
                stop.wait(interval)
        worker = threading.Thread(target=recover, name="booking-recovery", daemon=True)
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if start_worker:
                worker.join(timeout=10)
            if owned:
                actual.client.close()

    app = FastAPI(title="Flight Booking", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        return http_error(error.code, error.message)

    @app.exception_handler(grpc.RpcError)
    async def grpc_error(request, error):
        return http_error(error.code(), error.details())

    @app.exception_handler(psycopg.Error)
    async def database_error(request, error):
        log.error("booking_database_error", exc_info=error)
        return JSONResponse(status_code=503, content={"detail":"Booking database temporarily unavailable"})

    @app.get("/health")
    def health():
        with connect(app.state.service.dsn) as conn:
            conn.execute("SELECT 1")
        return {"status":"ok"}

    @app.get("/flights")
    def search_flights(origin: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
                       destination: Annotated[str, Query(pattern=r"^[A-Z]{3}$")], date: date | None = None):
        return [flight_json(f) for f in app.state.service.client.search(origin,destination,date).flights]

    @app.get("/flights/{id}")
    def get_flight(id: UUID):
        return flight_json(app.state.service.client.get_flight(str(id)))

    @app.post("/bookings", status_code=201)
    def create_booking(body: CreateBooking):
        return app.state.service.create(body.model_dump(mode="json"))

    @app.get("/bookings")
    def list_bookings(user_id: Annotated[str, Query(min_length=1,max_length=200,pattern=r"\S")]):
        return app.state.service.list(user_id)

    @app.get("/bookings/{id}")
    def get_booking(id: UUID):
        return app.state.service.get(str(id))

    @app.post("/bookings/{id}/cancel")
    def cancel_booking(id: UUID):
        return app.state.service.cancel(str(id))

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8080)
