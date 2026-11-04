import hmac
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from functools import wraps
import logging
import os
import signal
import grpc
import psycopg
from common.db import migrate
from common.errors import BusinessError, fail
from common.validation import uuid_string, iata, money
from generated import flight_pb2 as pb, flight_pb2_grpc as rpc_pb
from flight_service.repository import FlightRepository
from flight_service.cache import FlightCache, redis_from_env

log = logging.getLogger(__name__)


def rpc_errors(method):
    @wraps(method)
    def call(self, request, context):
        try:
            return method(self, request, context)
        except BusinessError as error:
            context.abort(error.code, error.message)
        except psycopg.Error:
            log.exception("flight_database_error method=%s", method.__name__)
            context.abort(grpc.StatusCode.UNAVAILABLE, "Flight database temporarily unavailable")
        except Exception:
            log.exception("flight_internal_error method=%s", method.__name__)
            context.abort(grpc.StatusCode.INTERNAL, "Flight service internal error")
    return call


def flight_message(row):
    result = pb.Flight(id=str(row["id"]), flight_number=row["flight_number"],
        airline=row["airline"], origin=row["origin"], destination=row["destination"],
        total_seats=row["total_seats"], available_seats=row["available_seats"],
        price=format(row["price"], ".2f"), status=pb.FlightStatus.Value(row["status"]))
    result.departure_at.FromDatetime(row["departure_at"])
    result.arrival_at.FromDatetime(row["arrival_at"])
    return result


def reservation_message(row):
    result = pb.SeatReservation(id=str(row["id"]), booking_id=str(row["booking_id"]),
        flight_id=str(row["flight_id"]), seat_count=row["seat_count"],
        status=pb.ReservationStatus.Value(row["status"]), unit_price=format(row["unit_price"], ".2f"))
    result.created_at.FromDatetime(row["created_at"])
    return result


class AuthInterceptor(grpc.ServerInterceptor):
    def __init__(self, key):
        if not key:
            raise ValueError("GRPC_API_KEY must not be empty")
        self.key = key

    def intercept_service(self, continuation, details):
        handler = continuation(details)
        provided = [value for name, value in details.invocation_metadata if name == "x-api-key"]
        if len(provided) == 1 and hmac.compare_digest(provided[0], self.key):
            return handler
        def reject(request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid service credentials")
        return grpc.unary_unary_rpc_method_handler(reject,
            request_deserializer=handler.request_deserializer if handler else None,
            response_serializer=handler.response_serializer if handler else None)


class FlightService(rpc_pb.FlightServiceServicer):
    def __init__(self, repo, cache):
        self.repo, self.cache = repo, cache

    def _read(self, key, message_type, loader):
        with self.repo.snapshot() as conn:
            revision = self.repo.revision(conn)
            cached = self.cache.get(key, revision, message_type)
            if cached is not None:
                return cached
            result = loader(conn)
        self.cache.put(key, revision, result)
        return result

    @rpc_errors
    def GetFlight(self, request, context):
        id = uuid_string(request.id)
        return self._read(f"flight:{id}", pb.Flight,
            lambda conn: flight_message(self.repo.get(conn, id)))

    @rpc_errors
    def SearchFlights(self, request, context):
        origin, destination = iata(request.origin), iata(request.destination)
        if origin == destination:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "Origin and destination must differ")
        date = None
        if request.HasField("date"):
            try:
                date = request.date.ToDatetime(tzinfo=timezone.utc)
                if date.hour or date.minute or date.second or date.microsecond:
                    raise ValueError()
            except (ValueError, OverflowError):
                fail(grpc.StatusCode.INVALID_ARGUMENT, "Search date must be a valid UTC midnight")
        key = f"search:{origin}:{destination}:{date.date().isoformat() if date else 'all'}"
        return self._read(key, pb.SearchFlightsResponse, lambda conn:
            pb.SearchFlightsResponse(flights=[flight_message(row) for row in self.repo.search(conn, origin, destination, date)]))

    @rpc_errors
    def ReserveSeats(self, request, context):
        fid = uuid_string(request.flight_id, "flight_id")
        bid = uuid_string(request.booking_id, "booking_id")
        if request.seat_count <= 0:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "seat_count must be positive")
        price = money(request.expected_unit_price) if request.expected_unit_price else None
        return reservation_message(self.repo.reserve(fid, request.seat_count, bid, price))

    @rpc_errors
    def ReleaseReservation(self, request, context):
        return pb.ReleaseReservationResponse(released=self.repo.release(uuid_string(request.booking_id, "booking_id")))

    @rpc_errors
    def UpdateFlight(self, request, context):
        id = uuid_string(request.id)
        price = money(request.price) if request.HasField("price") else None
        capacity = request.total_seats if request.HasField("total_seats") else None
        status = None
        if capacity is not None and capacity <= 0:
            fail(grpc.StatusCode.INVALID_ARGUMENT, "total_seats must be positive")
        if request.HasField("status"):
            if request.status not in (pb.SCHEDULED, pb.DEPARTED, pb.CANCELLED, pb.COMPLETED):
                fail(grpc.StatusCode.INVALID_ARGUMENT, "Unknown flight status")
            status = pb.FlightStatus.Name(request.status)
        return flight_message(self.repo.update(id, price, capacity, status))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dsn = os.environ["FLIGHT_DATABASE_URL"]
    key = os.environ["GRPC_API_KEY"]
    migrate(dsn, "flight")
    server = grpc.server(ThreadPoolExecutor(max_workers=16), interceptors=[AuthInterceptor(key)])
    rpc_pb.add_FlightServiceServicer_to_server(FlightService(FlightRepository(dsn),
        FlightCache(redis_from_env(), int(os.getenv("CACHE_TTL_SECONDS", "300")))), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    def stop(*_):
        server.stop(5)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("flight_service_started port=50051")
    server.wait_for_termination()


if __name__ == "__main__":
    main()
